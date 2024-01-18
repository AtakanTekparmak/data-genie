import argparse
import logging
import torch
import json
import re
import ast
import xml.etree.ElementTree as ET
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer

class ModelEvaluator:
    def __init__(self, model_path):
        logging.getLogger("transformers.tokenization_utils_base").setLevel(logging.ERROR)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            trust_remote_code=True,
            return_dict=True,
            torch_dtype=torch.float16,
            device_map="auto"
        )
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.tokenizer.pad_token = self.tokenizer.eos_token
        self.tokenizer.padding_side = "left"
        self.eval_results = []

    def validate_and_extract_tool_calls(self, completion):
        # Define a pattern to find the assistant message
        assistant_pattern = re.compile(r'<\|assistant\|>((?:(?!<\|assistant\|>|</s>).)*)</s>', re.DOTALL)
        assistant_match = assistant_pattern.search(completion)

        validation_result = False
        extracted_data = []
        if assistant_match:
            assistant_content = assistant_match.group(1).strip()
            print(assistant_content)

            try:
                # Wrap the assistant content with a root element
                xml_content = f"<root>{assistant_content}</root>"

                # Parse the assistant content as XML
                root = ET.fromstring(xml_content)

                # Iterate over all <tool_call> elements
                for tool_call_element in root.findall(".//tool_call"):
                    json_text = tool_call_element.text.strip()

                    try:
                        # Prioritize json.loads for better error handling
                        json_data = json.loads(json_text)  # Use json.loads first
                    except json.JSONDecodeError:
                        try:
                            # Fallback to ast.literal_eval if json.loads fails
                            json_data = ast.literal_eval(json_text)
                        except SyntaxError as err:
                            print(f"JSON parsing failed with both json.loads and ast.literal_eval:")
                            print(f"- JSON Decode Error: {err}")
                            print(f"- Problematic JSON text: {json_text}")
                            validation_result = False
                            continue  # Skip to the next tool_call_element

                    extracted_data.append(json_data)
                    validation_result = True

                return validation_result, extracted_data

            except ET.ParseError as xml_error:
                print(f"XML Parse Error: {xml_error}")
                return validation_result, extracted_data

        else:
            print("No match found for the assistant pattern.")
            return validation_result, extracted_data
        
    def validate_func_calls(self, generated_arguments, expected_arguments):
        for key, expected_value in expected_arguments.items():
            if generated_arguments.get(key) != expected_value:
                print(f"Function args do not match; expected:{expected_value}, got:{generated_arguments.get(key)}")
                return "failed"
        return "passed"

    def evaluate_dataset(self, eval_dataset):

        for sample in eval_dataset:
            #prompt = [
            #    {'role': 'system', 'content': sample["system"]},
            #    {'role': 'user', 'content': sample["user"]}
            #]
            inputs = self.tokenizer.apply_chat_template(
                sample['prompt'],
                add_generation_prompt=True,
                return_tensors='pt'
            )

            tokens = self.model.generate(
                inputs.to(self.model.device),
                max_new_tokens=512,
                temperature=0.1,
                do_sample=True
            )

            completion = self.tokenizer.decode(tokens[0], skip_special_tokens=False)

            validation, assistant_message = self.validate_and_extract_tool_calls(completion)
            print(assistant_message)

            if validation:
                function_found = False
                eval_tool_calls = json.loads(sample['completion'])
                for tool_call in assistant_message:
                    if tool_call['name'] == eval_tool_calls['name']:
                        result = self.validate_func_calls(tool_call['arguments'], eval_tool_calls['arguments'])
                        print(result)
                        function_found = True
                        break

                if not function_found:
                    print("Function not found")
                    result = "failed"
                    print(result)
            else:
                print("function call validation failed")
                result = "failed"
                print(result)

            sample['model_completion'] = assistant_message
            sample['result'] = result

            self.eval_results.append(sample)
    
    def calculate_pass_rate(self):
        passed_count =sum(1 for sample in self.eval_results if sample["result"] == "passed")
        pass_rate = passed_count / len(self.eval_results)
        return pass_rate

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate model performance on fireworks-ai dataset")
    parser.add_argument("model_path", type=str, help="Path to the model folder")
    args = parser.parse_args()
    
    # Load evaluation dataset
    eval_dataset = load_dataset("NousResearch/func-calling-eval")['train']

    # Create model evaluator instance
    model_evaluator = ModelEvaluator(args.model_path)

    # Evaluate the dataset
    model_evaluator.evaluate_dataset(eval_dataset)
    results_path = '/home/interstellarninja/ai_projects/axolotl/examples/stablelm/eval_results.json'
    with open(results_path, 'w') as file:
        json.dump(model_evaluator.eval_results, file)

    # Calculate and print pass rate
    pass_rate = model_evaluator.calculate_pass_rate()
    print(f"fireworks-ai function-calling eval (pass@1): {pass_rate}")
