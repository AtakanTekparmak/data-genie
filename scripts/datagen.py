import os
import re
import utils
import time
import yaml
import csv
import json
import argparse
import datetime
import threading
import concurrent.futures
from itertools import islice
from tenacity import retry, stop_after_attempt, wait_random_exponential
from langchain.schema import Document

from aiutilities import AIUtilities
from schema import OutputSchema
from promptmanager import PromptManager
from search import WebSearch
from vectordb import VectorDB

from dotenv import load_dotenv
load_dotenv()

import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Create a file handler and set the logging level
file_handler = logging.FileHandler('generator.log')
file_handler.setLevel(logging.DEBUG)  # Set the desired logging level for the file handler

# Create a console handler and set the logging level
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)  # Set the desired logging level for the console handler

# Create a formatter and attach it to the handlers
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Add the handlers to the logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

class DataGenPipeline:
    def __init__(self, config_path):
        # load config
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        self.ai_utilities = AIUtilities()
        self.web_search_client = WebSearch()
        self.vector_db = None
        self.file_write_lock = threading.Lock()

    def retrieve_and_combine_documents(self, query, num_results, folder_path, char_limit):
        # Check if the folder already exists
        if os.path.exists(folder_path) and os.listdir(folder_path):
            # Read from existing JSON files
            search_results = utils.read_documents_from_folder(folder_path, num_results)
        else:
            # Fetch new search results
            # Retrieve Google search results
            google_results = self.web_search_client.google_search(query, num_results)
            # Combine results to avoid duplicate URLs
            combined_results = [url for url in google_results]
            
            try:
                bing_results = self.web_search_client.bing_web_search(query, num_results)
                # Add Bing results without duplicate URLs to the combined results
                for url in bing_results:
                    if url not in combined_results:
                        combined_results.append(url)
            except Exception as e:
                logger.info(f"Could not complete bing search: {e}")
           
            # Fetch and save new search results
            search_results = self.web_search_client._scrape_results_parallel(combined_results)
            utils.save_search_results(folder_path, search_results)
            logger.info(f"Search results saved successfully at {folder_path}")

        try:
            combined_text = utils.combine_search_result_documents(search_results, char_limit)
            return combined_text
        except Exception as e:
            return f"Exception in the loop: {e}"
    
    def retrieve_and_combine_examples(self, query, results_path, num_examples=2):
        # Create an instance of the VectorDB class

        if not self.vector_db:
            self.vector_db = VectorDB()
            schema_path = self.config["paths"]["redis_schema"]
            try:     
                self.vector_db.load_vector_store(schema_path)
            except Exception as e:
                logger.info(f"Couldn't load existing index: {e}")
                examples_path = self.config["paths"]["examples_path"]
                self.vector_db.initialize_vector_store(examples_path, schema_path)
                if os.listdir(results_path):
                    documents = self.vector_db.load_documents_from_folder(results_path)
                    self.vector_db.rds.add_documents(documents)

        retrieved_docs = self.vector_db.perform_similarity_search(query, num_examples)
        combined_examples = utils.combine_examples(retrieved_docs)

        return combined_examples


    def extract_and_save_results(self, file_path, completion, task_desc):
        try:
            # Try loading the completion as JSON
            try:
                json_object = json.loads(completion)
            except json.JSONDecodeError:
                # If loading as JSON fails, call extract_json_from_response
                json_object = utils.extract_json_from_response(completion)

            # Check if the JSON object is empty
            if not json_object:
                raise ValueError("Completion contains an empty JSON object")

            with self.file_write_lock:
                with open(file_path, 'w') as json_file:
                    json.dump(json_object, json_file, indent=2)
                logger.debug(f"Lock released for {task_desc}")

            logger.info(f"Results for {task_desc} saved successfully at {file_path}")

            # index the result to vectordb for example selection
            document = Document(
                page_content=completion,
                metadata={
                    "source": file_path
                }
            )
            self.vector_db.rds.add_documents([document])

        except Exception as e:
            logger.debug(f"Error extracting and saving results for {task_desc}: {str(e)}")
        finally:
            # Ensure that the lock is always released
            self.file_write_lock.release()
    
    @retry(wait=wait_random_exponential(multiplier=1, max=30), stop=stop_after_attempt(3))
    def run_data_generation(self, task, query, ai_vendor, num_results):
        # search results folder path
        today_date = datetime.date.today()
        folder_name = f"search_results/{today_date}"
        folder_path = os.path.join(os.getcwd(), folder_name, task[0], task[1], task[2])
        folder_path = folder_path.replace(' ', '_')
        os.makedirs(folder_path, exist_ok=True)
        
         # Create a folder for each task if it doesn't exist
        task_desc = f"{task[0]}_{task[1]}_{task[2]}"
        task_desc = task_desc.replace(' ', '_')
        results_path = self.config["paths"]["results_path"]
        results_path = f"{results_path}/{ai_vendor}_{today_date}"
        results_path = os.path.join(os.getcwd(), results_path, task[0], task[1])
        results_path = results_path.replace(' ', '_')
        os.makedirs(results_path, exist_ok=True)

        file_path = os.path.join(results_path, f"{task[2]}.json")
        file_path = file_path.replace(' ', '_')
        # Check if the file already exists
        if not os.path.exists(file_path):
            ctx_len = self.ai_utilities.get_ai_context_length(ai_vendor)
            char_limit = (int(ctx_len) - 8000) * 4
            logger.info(f"The character limit for documents is:{char_limit}")
            combined_documents = self.retrieve_and_combine_documents(query, num_results, folder_path, char_limit)
            combined_examples = self.retrieve_and_combine_examples(query, results_path, num_examples=3)

            # Set variables for prompt YAML
            variables = {
                "category": task[0],
                "subcategory": task[1],
                "task": task[2],
                "doc_list": combined_documents,
                "examples": combined_examples,
                "pydantic_schema": OutputSchema.schema_json(),
            }

            prompt_manager = PromptManager(self.config)
            prompt = prompt_manager.generate_prompt(variables)
            logger.info(f"Logging prompt text\n{prompt}")
            
            completion = self.ai_utilities.run_ai_completion(prompt, ai_vendor)
            logger.info(f"Here's the generated json output:\n{completion}")
            # Extract and save results for each task
            logger.info(f"saving json files for {task_desc}")
            self.extract_and_save_results(file_path, completion, task_desc)

            return completion
        else:
            return f"Data already generated for the {task_desc}"
    
    def run_generation_pipeline(self, ai_vendor="openai", num_results=10, num_tasks=5):
        curriculum_csv_path = self.config["paths"]["curriculum_csv"]
        with open(curriculum_csv_path, 'r') as csv_file:
            reader = csv.DictReader(csv_file)
            tasks = [(row['Category'], row['SubCategory'], row['Task']) for row in islice(reader, num_tasks)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_task = {executor.submit(self.run_data_generation, task, utils.generate_query(*task), ai_vendor, num_results): task for task in tasks}

            for future in concurrent.futures.as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    completion = future.result()
                    logger.info(f"Category: {task[0]}, SubCategory: {task[1]}, Task: {task[2]}")
                    logger.info("Completion: {}".format(completion))
                except Exception as e:
                    logger.error(f"Error processing task {task[0]}: {str(e)}")
                # Introduce a small delay between tasks (e.g., 0.1 seconds)
                time.sleep(0.1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run data generation pipeline")
    parser.add_argument("--ai_vendor", choices=["openai", "anthropic", "together", "anyscale"], default="openai", help="choose AI vendor (openai, anthropic, together, anyscale)")
    parser.add_argument("--num_results", type=int, default=10, help="Number of top-k documents for search results")
    parser.add_argument("--num_tasks", type=int, default=10, help="Number of tasks to generate data for")

    args = parser.parse_args()

  # Example usage for running analysis for companies in a CSV file
    config_path = "./config.yaml"
    datagen = DataGenPipeline(config_path)
    datagen.run_generation_pipeline(ai_vendor=args.ai_vendor, num_results=args.num_results, num_tasks=args.num_tasks)
