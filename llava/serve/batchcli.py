import argparse
import torch
import csv
import os

from llava.constants import IMAGE_TOKEN_INDEX, DEFAULT_IMAGE_TOKEN, DEFAULT_IM_START_TOKEN, DEFAULT_IM_END_TOKEN
from llava.conversation import conv_templates, SeparatorStyle
from llava.model.builder import load_pretrained_model
from llava.utils import disable_torch_init
from llava.mm_utils import process_images, tokenizer_image_token, get_model_name_from_path

from PIL import Image

import requests
from PIL import Image
from io import BytesIO
from transformers import TextStreamer


def load_image(image_file):
    if image_file.startswith('http://') or image_file.startswith('https://'):
        response = requests.get(image_file)
        image = Image.open(BytesIO(response.content)).convert('RGB')
    else:
        image = Image.open(image_file).convert('RGB')
    return image

def write_to_csv(model_path, model_base, image_file, prompt, response):
    file_exists = os.path.isfile('output.csv')
    with open('output.csv', mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(['Model Path', 'Model Base', 'Image File URL', 'Prompt', 'Response'])
        writer.writerow([model_path, model_base or "", image_file, prompt, response])

def main(args):
    # Model
    disable_torch_init()

    model_name = get_model_name_from_path(args.model_path)
    tokenizer, model, image_processor, context_len = load_pretrained_model(args.model_path, args.model_base, model_name, args.load_8bit, args.load_4bit, device=args.device)

    if "llama-2" in model_name.lower():
        conv_mode = "llava_llama_2"
    elif "mistral" in model_name.lower():
        conv_mode = "mistral_instruct"
    elif "v1.6-34b" in model_name.lower():
        conv_mode = "chatml_direct"
    elif "v1" in model_name.lower():
        conv_mode = "llava_v1"
    elif "mpt" in model_name.lower():
        conv_mode = "mpt"
    else:
        conv_mode = "llava_v0"

    if args.conv_mode is not None and conv_mode != args.conv_mode:
        print('[WARNING] the auto inferred conversation mode is {}, while `--conv-mode` is {}, using {}'.format(conv_mode, args.conv_mode, args.conv_mode))
    else:
        args.conv_mode = conv_mode

    prompts = []
    with open(args.prompt_file, 'r') as prompt_file:
        prompts = [line.strip() for line in prompt_file]

    with open(args.image_file_list, 'r') as file:
        for image_file in file:
            image_file = image_file.strip()
            image = load_image(image_file)
            image_size = image.size
            image_tensor = process_images([image], image_processor, model.config)

            if isinstance(image_tensor, list):
                image_tensor = [img.to(model.device, dtype=torch.float16) for img in image_tensor]
            else:
                image_tensor = image_tensor.to(model.device, dtype=torch.float16)

            for prompt in prompts:
                # Reset the conversation for each image and prompt
                conv = conv_templates[args.conv_mode].copy()
                roles = conv.roles

                if image is not None:
                    if model.config.mm_use_im_start_end:
                        prompt = DEFAULT_IM_START_TOKEN + DEFAULT_IMAGE_TOKEN + DEFAULT_IM_END_TOKEN + '\n' + prompt
                    else:
                        prompt = DEFAULT_IMAGE_TOKEN + '\n' + prompt

                conv.append_message(conv.roles[0], prompt)
                conv.append_message(conv.roles[1], None)
                prompt_text = conv.get_prompt()

                input_ids = tokenizer_image_token(prompt_text, tokenizer, IMAGE_TOKEN_INDEX, return_tensors='pt').unsqueeze(0).to(model.device)
                stop_str = conv.sep if conv.sep_style != SeparatorStyle.TWO else conv.sep2
                streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)

                try:
                    with torch.inference_mode():
                        output_ids = model.generate(
                            input_ids,
                            images=image_tensor,
                            image_sizes=[image_size],
                            do_sample=True if args.temperature > 0 else False,
                            temperature=args.temperature,
                            max_new_tokens=args.max_new_tokens,
                            streamer=streamer,
                            use_cache=True)

                    outputs = tokenizer.decode(output_ids[0]).strip()
                    conv.messages[-1][-1] = outputs
                    print(f"{roles[1]}: {outputs}")

                    # Write to CSV
                    write_to_csv(args.model_path, args.model_base if args.model_base else "", image_file, prompt, outputs)

                    if args.debug:
                        print("\n", {"prompt": prompt_text, "outputs": outputs}, "\n")
                except Exception as e:
                    print(f"Error processing image {image_file} with prompt '{prompt}': {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=str, default="facebook/opt-350m")
    parser.add_argument("--model-base", type=str, default=None)
    parser.add_argument("--image-file-list", type=str, required=True)
    parser.add_argument("--prompt-file", type=str, required=True)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--conv-mode", type=str, default=None)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--load-8bit", action="store_true")
    parser.add_argument("--load-4bit", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    main(args)