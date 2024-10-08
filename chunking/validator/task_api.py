import time
from typing import Optional, List, Tuple
from venv import logger
import bittensor as bt
import tiktoken
from chunking.protocol import chunkSynapse
import requests
import numpy as np
from sr25519 import sign
import json
import os
from random import choices
from math import ceil

from neurons.validator import Validator

class Task():
    def __init__(
        self,
        synapse: chunkSynapse,
        task_type: str,
        task_id: int,
        miner_uids: Optional[List[int]] = None,
    ):
        self.synapse = synapse
        self.task_type = task_type
        self.task_id = task_id
        self.miner_uids = miner_uids
    @classmethod
    def get_new_task(self, validator: Validator):

        if os.environ.get('ALLOW_ORGANIC_CHUNKING_QUERIES') == 'True':
            hotkey = validator.wallet.get_hotkey()
            nonce = validator.step
            data = {
                'hotkey_address': hotkey.ss58_address,
                'nonce': nonce
            }

            # sign request with validator hotkey
            request_signature = sign(
                (hotkey.public_key, hotkey.private_key),
                str.encode(json.dumps(data))
                ).hex()

            API_host = os.environ['CHUNKING_API_HOST']
            task_url = f"{API_host}/task_api/get_new_task/"
            headers = {"Content-Type": "application/json"}
            request_data = {
                'data': data, 
                'signature': request_signature
                }
            try:
                response = requests.post(url=task_url, headers=headers, json=request_data)
                if response.status_code == 502:
                    raise Exception(f"API Host: \'{API_host}\' is down")
                elif response.status_code == 403:
                    raise Exception(response.text())
                elif response.status_code != 200:
                    raise Exception(f"Post to API failed with status code: {response.status_code}")
                else:
                    task = response.json()
                    if task["task_id"] != -1:
                        task_id = task["task_id"]
                        miner_uids = task.get('miner_uids')
                        bt.logging.debug(f"Received organic query with task id: {task_id}")
                        if task["timeout"] == None:
                            task["timeout"] = 5.0
                        if task["chunk_size"] == None:
                            task["chunk_size"] = 4096
                        if task["chunk_qty"] == None:
                            task["chunk_qty"] = ceil(
                                ceil(len(task["document"]) / task["chunk_size"]) * 1.5
                            )
                        synapse = chunkSynapse(
                            document=task["document"],
                            timeout=task["timeout"],
                            chunk_size=task["chunk_size"],
                            chunk_qty=task["chunk_qty"],
                        )
                return Task(synapse=synapse, task_type="organic", task_id=task_id, miner_uids=miner_uids), -1
            except Exception as e:
                bt.logging.error(f"Failed to get task from API host: \'{API_host}\'. Exited with exception\n{e}")
        bt.logging.debug("Generating synthetic query")
        synapse, page = generate_synthetic_synapse(validator)
        return Task(synapse=synapse, task_type="synthetic", task_id=-1), page

    @classmethod
    def return_response(cls, validator, response_data):
        validator_hotkey = validator.wallet.get_hotkey()
        validator_sig = sign(
            (validator_hotkey.public_key, validator_hotkey.private_key),
            str.encode(json.dumps(response_data))
            ).hex()
        API_host = os.environ['CHUNKING_API_HOST']
        task_url = f"{API_host}/task_api/organic_response/"
        headers = {"Content-Type": "application/json"}
        data = {
            'response_data': response_data,
            'validator_sig': validator_sig,
        }
        try:
            response = requests.post(task_url, headers=headers, json=data)
        except Exception as e:
            bt.logging.error(f"Failed to return response to API host: \'{API_host}\'. Exited with exception\n{e}")


    @classmethod
    def upload_logs(cls, validator, log_data):
        hotkey = validator.wallet.get_hotkey()
        signature = sign(
            (hotkey.public_key, hotkey.private_key),
            str.encode(json.dumps(log_data))
            ).hex()
            
        API_host = os.environ['CHUNKING_API_HOST']
        task_url = f"{API_host}/task_api/log/" 
        headers = {"Content-Type": "application/json"}
        data = {
            'log_data': log_data,
            'signature': signature,
        }
        try:
            response = requests.post(task_url, headers=headers, json=data)            
            bt.logging.debug(f"upload_logs: response: {response.status_code}")
            
            if response.status_code == 502:
                raise Exception(f"API Host: \'{API_host}\' is down")
            elif response.status_code == 403:
                raise Exception(response.text())
            elif response.status_code != 200:
                raise Exception(f"Post to API failed with status code: {response.status_code}")
            else:
                bt.logging.debug(f"Successfully uploaded logs to API host: \'{API_host}\'")            
                            
        except Exception as e:
            bt.logging.error(f"Failed to upload logs to API host: \'{API_host}\'. Exited with exception\n{e}")

def num_tokens_from_string(string: str, encoding_name: str) -> int:
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens

def generate_synthetic_synapse(validator, pageids = None, timeout = 20) -> Tuple[chunkSynapse, int]:
    pages = choices(validator.articles, k=3) if pageids == None or len(pageids) < 3 else pageids
    source_articles = []
    for page in pages:
        source_articles.append(requests.get('https://en.wikipedia.org/w/api.php', params={
            'action': 'query',
            'format': 'json',
            'pageids': page,
            'prop': 'extracts',
            'explaintext': True,
            'exsectionformat': 'plain',
        }).json()['query']['pages'][str(page)]['extract'])
    system_prompt = "You are a writer tasked with writing an article that combines multiple topics. You are known for your long-winded tangents and detailed exploration of all topics covered in your articles."
    
    bt.logging.debug(f"source pageids: {pages}")
    
    bt.logging.info("Generating first half of synthetic query")
    start = time.time()
    
    first_half = validator.client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.7,    
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"""
                Use the following three articles to write the first half of an article that will be between 2,000 and 5,000 words long. Do not include section titles. Write to your token limit.
                Article 1:
                {source_articles[0]}
            
                Article 2:
                {source_articles[1]}

                Article 3:
                {source_articles[2]}
                """
            }
        ]
    ).choices[0].message.content

    bt.logging.info(f"Generated first half of synthetic query in {time.time() - start} seconds")
    bt.logging.info("Generating second half of synthetic query")

    start_2 = time.time()
    
    second_half = validator.client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.7,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"""
                This is the first half of an article that will be betwen 2,000 and 5,000 words long when completed:
                {first_half}
                Continue the article. Do not include section titles. Write to your token limit.
                """
            }
        ]).choices[0].message.content
    
    bt.logging.info(f"Generated second half of synthetic query in {time.time() - start_2} seconds") 
    
    num_chars = len(first_half) + len(second_half)
    
    bt.logging.info(f"Generated synthetic query with {num_chars} characters")  
    
    num_tokens = num_tokens_from_string(first_half + second_half, "o200k_base")
    
    bt.logging.info(f"Generated synthetic query with {num_tokens} tokens")  
       
    document = " ".join([first_half, second_half])
    document = " ".join(document.split())    
    
    bt.logging.info(f"Took {time.time() - start} seconds to generate synthetic query")
    
    timeout = validator.config.neuron.timeout if validator is not None else timeout
    time_soft_max = timeout * 0.75
    chunk_size = 4096
    chunk_qty = ceil(
        ceil(len(document) / chunk_size) * 1.5
    )
    synapse = chunkSynapse(
        document=document,
        time_soft_max=time_soft_max,
        chunk_size=chunk_size,
        chunk_qty=chunk_qty,
        timeout=timeout
    )
    return synapse, page
