import asyncio
import random
import uuid
import aiohttp
import sys
import os
from datetime import date, datetime
from ethereum_utils.nft_generator import Generator
import json

import pytz
from handlers.handler import Handler
from json import JSONDecodeError
from utils import json_response
from connection import rabbitmq_connection as rmq
from aiohttp import web
from models.RMQModels import AlgorithmExchangeMessage

from PIL import Image
from io import BytesIO
import base64
import logging
import time
from multiprocessing import Lock

logger = logging.getLogger('aiohttp')

class HacktmHandler(Handler):
    def __init__(self, env, db_connection, config):
        super(HacktmHandler, self).__init__(env, db_connection)
        self.env = env
        self.db_connection = db_connection

        self.answer_dict = {}
        self.answer_dict_lock = Lock()

        loop = asyncio.get_event_loop()

        # get config if listen, send to cloud
        self.rmq_config = config['RabbitMQ'].get(self.env, config['RabbitMQ']['default_options'])
        logger.info('Diagnosis Handler configured...')
        if self.rmq_config['listen']:
            loop.create_task(
                self.process_algorithms_queue_thread(
                    loop.run_until_complete(rmq.init(env, 'ai_algorithms_response'))
                )
            )

        # loop.run_forever()

    async def process_algorithms_queue_thread(self, queue):
        logger.info('Listening for message in queue')

        answers_dict = {
            1: 'leaf_segmentation',
            2: 'tree_detection',
            3: 'tree_classification'
        }

        async for message in queue:
            with message.process():
                data = json.loads(message.body)

                at = data['answer_type']
                # 1, data['id'], bbox_path, leaf_path
                if at not in answers_dict.keys():
                    logger.warn(f'Weird answer type for {data}, skipping')
                    continue

                with self.answer_dict_lock:
                    if data['id'] not in self.answer_dict:
                        self.answer_dict[data['id']] = {}

                    if at == 1:
                        self.answer_dict[data['id']][answers_dict[at]] = {
                            'bbox_path': data['bbox_path'],
                            'black_image': data['leaf_path'],
                        }
                    elif at == 2:
                        self.answer_dict[data['id']][answers_dict[at]] = {
                            'coord_om': data['coord_om'],
                            'coord_copac': data['coord_copac'],
                            # 'bbox_path': data['bbox_path'],
                        }

                    logger.info(f'Received message: {self.answer_dict}')
                    

    async def detect_tree(self, request):
        img_id = str(uuid.uuid4())

        a = time.time()
        try:
            reader = await request.multipart()
            logger.info(f'Received requests {img_id}')

            image_uploaded = await reader.next()

            if image_uploaded is None:
                raise ValueError(f'{img_id}: No images in the request')

            # # check if file filed exist
            if 'file' not in image_uploaded.name:
                raise AttributeError(f'{img_id}: Wrong field name for part')

            current_time = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            request_dir = os.path.join(self.FS_PATH, current_time + '/')
            img_path = f'{request_dir}/img.jpeg'

            if not os.path.isdir(request_dir):
                os.makedirs(request_dir)

            with open(f'{img_path}', 'wb') as f:
                while True:
                    chunk = await image_uploaded.read_chunk(size=8192)
                    if not chunk:
                        break
                    f.write(chunk)

            logger.info(f"{img_id}: Image from {image_uploaded.name} uploaded to {img_path} in {time.time() - a:.2f}s")
        except Exception as e:
            logger.exception(f'Error receiving image')
            return json_response({'message': f'Error receiving image: {e}'}, status=400)

        # leaf_doc = {
        #     'id':  img_id,
        #     'time': date.today().strftime("%Y-%m-%d"),
        #     'path': img_path,
        # }

        # await self.db_connection.save_to_db('segmentation_entries', leaf_doc)
        # logging.info(f'{img_id} saved in db')

        logger.info(f'{img_id} trying to send to rabbit')
        message = AlgorithmExchangeMessage(img_path=img_path, id=img_id, output_dir=request_dir)
        await rmq.publish_message(exchange_name='tree_detection_exchange', message=message.get_json(), env=self.env)
        logger.info(f'{img_id} sent to rabbit')

        a = time.time()
        while True:
            await asyncio.sleep(1)
            with self.answer_dict_lock:
                if 'tree_detection' in self.answer_dict.get(img_id, {}):
                    logger.info(f'Detect Tree done in {time.time() - a:.2f}s')
                    self.answer_dict[img_id]['tree_detection']['bbox_image'] = f'https://file.plant2win.com/{current_time}/tree_detection.jpg'
                    return json_response(self.answer_dict[img_id]['tree_detection'],  status=200)
                else:
                    logger.info(f'Tree detection not yet in dict')
            if time.time() - a > 15:
                return json_response({'message': f'Timeout whilst processing image'}, status=400)

    async def segment_leaf(self, request):
        img_id = str(uuid.uuid4())

        a = time.time()
        try:
            reader = await request.multipart()
            logger.info(f'Received requests {img_id}')

            image_uploaded = await reader.next()

            if image_uploaded is None:
                raise ValueError(f'{img_id}: No images in the request')

            # # check if file filed exist
            if 'file' not in image_uploaded.name:
                raise AttributeError(f'{img_id}: file wrong field name for part')

            current_time = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            request_dir = os.path.join(self.FS_PATH, current_time + '/')
            img_path = f'{request_dir}/img.jpeg'

            if not os.path.isdir(request_dir):
                os.makedirs(request_dir)

            with open(f'{img_path}', 'wb') as f:
                while True:
                    chunk = await image_uploaded.read_chunk(size=8192)
                    if not chunk:
                        break
                    f.write(chunk)

            lat = await reader.next()
            if 'lat' not in lat.name:
                raise AttributeError(f'{img_id}: lat wrong field name for part')
            
            lat = await lat.read()

            long = await reader.next()
            if 'long' not in long.name:
                raise AttributeError(f'{img_id}: long wrong field name for part')
            
            long = await long.read()

            logger.info(f"{img_id}: Image from {image_uploaded.name} uploaded to {img_path} in {time.time() - a:.2f}s")
        except Exception as e:
            logger.exception(f'Error receiving image')
            return json_response({'message': f'Error receiving image: {e}'}, status=400)

        logger.info(f'{img_id} trying to send to rabbit')
        message = AlgorithmExchangeMessage(img_path=img_path, id=img_id, output_dir=request_dir)
        await rmq.publish_message(exchange_name='leaf_segmentation_exchange', message=message.get_json(), env=self.env)
        logger.info(f'{img_id} sent to rabbit')

        a = time.time()
        while True:
            await asyncio.sleep(1)
            with self.answer_dict_lock:
                if 'leaf_segmentation' in self.answer_dict.get(img_id, {}):
                    logger.info(f'Leaf seg done in {time.time() - a:.2f}s')
                    leaf_seg_entry = self.answer_dict[img_id]['leaf_segmentation'].copy()
                    break
                else:
                    logger.info(f'Tree detection not yet in dict')
            if time.time() - a > 15:
                return json_response({'message': f'Timeout whilst processing image'}, status=400)

        # generate nft

        nft_path = f'https://file.plant2win.com/nft/0{random.randint(1, 8)}.jpg'
        nft_timestamp = str(datetime.fromtimestamp(time.time(), tz=pytz.timezone('Europe/Bucharest'))).replace(' ','Z').split('.')[0]

        node_url = "https://ropsten.infura.io/v3/c7d4a7dd4c414d8184e2e7acfe3a9057"
        private_key = "872f3dee80ae96e40856c01bdd91cf332e7845aa669e001d335262dd04714a80"
        signer_key = "872f3dee80ae96e40856c01bdd91cf332e7845aa669e001d335262dd04714a80" 
        contract_address= "0x08AeDa006B3BFAD92AED30f6C79192f9F5D87b4c"
        contract_abi = json.loads(open('ethereum_utils/abi.json').read().replace('\n', ''))

        generator = Generator(node_url, private_key, signer_key, contract_address,contract_abi)

        # store json ipfs
        ipfs_json = {
            "name": "Thor's hammer",
            "description": "Mjölnir, the legendary hammer of the Norse god of thunder.",
            "image": nft_path,
            "strength": 20
        }

        response = generator.generate_nft("0x09039B0ea5DA24cA9DD9C9ABDeCbbD6ef47C2bBA","ipfs://meh")
        logger.info(f'NFT created: {response}')

        nft_doc = {
            "id" : img_id,
            "username" : "Stefan",
            "location": "Dumbravita, Timis",
            "nft_url" : nft_path,
            "forest_name" : "Stefan's Forest",
            "price" : [ 
                {
                    "timestamp" : nft_timestamp,
                    "price" : "0.014"
                }, 
            ],
            "creation_time" : nft_timestamp,
            "co2_absorbtion" : "0.3T",
            "tree_type": "cherry"
        }

        await self.db_connection.save_to_db('nfts', nft_doc)
        logger.info(f'NFT {img_id} saved in db')

        leaf_seg_entry = {
            'bbox_path': f'https://file.plant2win.com/{current_time}/leaf_seg.jpg',
            'black_image': f'https://file.plant2win.com/{current_time}/leaf_only.jpg',
            'nft_entry': nft_doc,
        }

        return json_response(leaf_seg_entry,  status=200)

    async def retrieve_latest_nfts(self,request):
        nfts_answer = []
        nfts = self.db_connection.find('nfts', {'id':{'$exists':True}})
        async for doc in nfts:
            doc['_id'] = str(doc['_id'])
            nfts_answer.append(doc)
        return json_response(nfts_answer)
