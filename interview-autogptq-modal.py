import time
import json
from pathlib import Path
from modal import Image, Stub, method, gpu
from huggingface_hub import snapshot_download

def save_meta(name, base, safetensors = True, bits = 4, group = 128, actorder = True, eos = ['<s>', '</s>']):
    with open("/model/_info.json",'w') as f:
        json.dump({
            "model_name": name,
            "model_base": base,
            "model_safetensors": safetensors,
            "model_bits": bits,
            "model_group": group,
            "model_actorder": actorder,
            "model_eos": eos,
        }, f)

def download_wizardlm30b_nogroup_model_v2():   
    MODEL_NAME = "TheBloke/WizardLM-30B-Uncensored-GPTQ"
    MODEL_BASE = "WizardLM-30B-Uncensored-GPTQ-4bit.act-order"

    snapshot_download(local_dir=Path("/model"), repo_id=MODEL_NAME, allow_patterns=["*.json","*.model",MODEL_BASE+"*"])
    save_meta(MODEL_NAME, MODEL_BASE)

def download_wizardlm_1p0_30b_nogroup_model_v2():   
    MODEL_NAME = "TheBloke/WizardLM-30B-GPTQ"
    MODEL_BASE = "wizardlm-30b-GPTQ-4bit--1g.act.order"

    snapshot_download(local_dir=Path("/model"), repo_id=MODEL_NAME, allow_patterns=["*.json","*.model",MODEL_BASE+"*"])
    save_meta(MODEL_NAME, MODEL_BASE)

def download_falcon7b_v2():   
    MODEL_NAME = "TheBloke/falcon-7b-instruct-GPTQ"
    MODEL_BASE = "gptq_model-4bit-64g"

    snapshot_download(local_dir=Path("/model"), repo_id=MODEL_NAME, allow_patterns=["*.json","*.model","*.py",MODEL_BASE+"*"])
    save_meta(MODEL_NAME, MODEL_BASE, eos=['<|endoftext|>'])

def download_vicuna_1p1_13b_v2():   
    MODEL_NAME = "TheBloke/vicuna-13B-1.1-GPTQ-4bit-128g"
    MODEL_BASE = "vicuna-13B-1.1-GPTQ-4bit-128g.compat.no-act-order"

    snapshot_download(local_dir=Path("/model"), repo_id=MODEL_NAME, allow_patterns=["*.json","*.model",MODEL_BASE+"*"])
    save_meta(MODEL_NAME, MODEL_BASE, safetensors=False)

def download_wizardlm_1p0_13b_v2():   
    MODEL_NAME = "TheBloke/wizardLM-13B-1.0-GPTQ"
    MODEL_BASE = "WizardLM-13B-1.0-GPTQ-4bit-128g.no-act-order"

    snapshot_download(local_dir=Path("/model"), repo_id=MODEL_NAME, allow_patterns=["*.json","*.model",MODEL_BASE+"*"])
    save_meta(MODEL_NAME, MODEL_BASE, actorder=False)

def download_llama_30b_v2():   
    MODEL_NAME = "tsumeone/llama-30b-supercot-4bit-cuda"
    MODEL_BASE = "4bit"

    snapshot_download(local_dir=Path("/model"), repo_id=MODEL_NAME, allow_patterns=["*.json","*.model",MODEL_BASE+"*"])
    save_meta(MODEL_NAME, MODEL_BASE, bits=4, group=-1, actorder=True)

stub = Stub(name='autogptq-v2')
stub.gptq_image = (
    Image.from_dockerhub(
        "nvidia/cuda:11.7.1-devel-ubuntu22.04",
        setup_dockerfile_commands=[
            "RUN apt-get update",
            "RUN apt-get install -y python3 python3-pip python-is-python3 git build-essential",
        ],
    )
    .run_commands(
        "git clone https://github.com/PanQiWei/AutoGPTQ /repositories/AutoGPTQ",
        "cd /repositories/AutoGPTQ && pip install . && pip install einops sentencepiece && python setup.py install",
        gpu="any",
    )
    #.run_function(download_wizardlm30b_nogroup_model_v2)
    #.run_function(download_wizardlm_1p0_30b_nogroup_model_v2)
    #.run_function(download_falcon7b_v2)
    .run_function(download_wizardlm_1p0_13b_v2)    
    #.run_function(download_llama_30b_v2)
)

# Entrypoint import trick for when inside the remote container
if stub.is_inside(stub.gptq_image):
    t0 = time.time()
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning, message="TypedStorage is deprecated")
    import sys
    sys.path.insert(0, str(Path("/repositories/AutoGPTQ")))
    import torch
    from transformers import AutoTokenizer
    from auto_gptq import AutoGPTQForCausalLM
    from auto_gptq.modeling import BaseQuantizeConfig

#### NOTE: SET GPU TYPE HERE ####
@stub.cls(image=stub.gptq_image, gpu=gpu.A10G(count=1), concurrency_limit=1, container_idle_timeout=300)
class ModalGPTQ:
    def __enter__(self):
        quantized_model_dir = "/model"

        self.info = json.load(open('/model/_info.json'))
        print('Remote model info:', self.info)

        if not Path('/model/quantize_config.json').exists():
            quantize_config = BaseQuantizeConfig()
            quantize_config.desc_act = self.info['model_actorder']
            quantize_config.bits = self.info['model_bits']
            quantize_config.group = self.info['model_group']
            quantize_config.save_pretrained('/model')
        else:
            print('This model contains quantize_config.')

        print('Loading tokenizer...')
        tokenizer = AutoTokenizer.from_pretrained(quantized_model_dir, use_fast=False)

        print('Loading model...')
        model = AutoGPTQForCausalLM.from_quantized(quantized_model_dir, model_basename=self.info['model_base'], device_map="auto", load_in_8bit=True, use_triton=False, use_safetensors=self.info['model_safetensors'], torch_dtype=torch.float32, trust_remote_code=True)
        
        self.model = model
        self.tokenizer = tokenizer
        print(f"Model loaded in {time.time() - t0:.2f}s")

    def params(self, temperature=0.7, repetition_penalty=1.0, top_k=-1, top_p=1.0, max_new_tokens=512, **kwargs):
        return {
            "temperature": temperature,
            "repetition_penalty": repetition_penalty,
            "top_k": top_k,
            "top_p": top_p,
            "max_new_tokens": max_new_tokens
        }

    @method()
    def generate(self, prompt, params):
        tokens = self.tokenizer(prompt, return_tensors="pt").to("cuda:0").input_ids
        output = self.model.generate(input_ids=tokens, do_sample=True, **params)

        decoded = self.tokenizer.decode(output[0])

        # Remove the prompt and all special tokens
        answer = decoded.replace(prompt, '')
        for special_token in self.info['model_eos']:
            answer = answer.replace(special_token, '')

        return answer, self.info

# For local testing, run `modal run -q interview-gptq-modal.py --input questions.csv --params model_parameters/precise.json`
@stub.local_entrypoint()
def main(input: str, params: str, iterations: int = 1):
    from prepare import save_interview

    model = ModalGPTQ()

    interview = [json.loads(line) for line in open(input)]
    params_json = json.load(open(params,'r'))
    params_model = model.params(**params_json)
    model_info = None

    for iter in range(iterations):
        results = []
        for question in interview:
            print(question['name'], question['language'])

            # generate the answer
            answer, info = model.generate.call(question['prompt'], params=params_model)

            # save for later
            if model_info is None:
                model_info = info
                print('Local model info:', model_info)
            
            print()
            print(answer)
            print()

            result = question.copy()
            result['answer'] = answer
            result['params'] = params_model
            result['model'] = info['model_name']
            result['runtime'] = 'autogptq'
            results.append(result)

        save_interview(input, 'none', params, model_info['model_name'], results)