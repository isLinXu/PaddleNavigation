# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from paddle_serving_server.pipeline import PipelineClient
import numpy as np
import requests
import json
import cv2
import base64
import os

client = PipelineClient()
client.connect(['127.0.0.1:9993'])


def cv2_to_base64(image):
    return base64.b64encode(image).decode('utf8')


with open("daisy.jpg", 'rb') as file:
    image_data = file.read()
image = cv2_to_base64(image_data)

for i in range(1):
    ret = client.predict(feed_dict={"image": image}, fetch=["label", "prob"])
    print(ret)
