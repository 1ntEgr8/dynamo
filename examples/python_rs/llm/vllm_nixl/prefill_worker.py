# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import asyncio

import msgspec
import uvloop
from common import NixlMetadataStore, parse_vllm_args
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.entrypoints.openai.api_server import (
    build_async_engine_client_from_engine_args,
)
from vllm.inputs.data import TokensPrompt
from vllm.remote_prefill import RemotePrefillParams, RemotePrefillRequest
from vllm.logger import logger as vllm_logger

from dynemo.runtime import DistributedRuntime, dynemo_worker


class RequestHandler:
    def __init__(self, engine_client, metadata_store):
        self.engine_client = engine_client
        self._metadata_store = metadata_store
        self._loaded_metadata = set()
        vllm_logger.info("RequestHandler initialized")

    async def generate(self, raw_request: str):
        request: RemotePrefillRequest = msgspec.json.decode(
            raw_request.encode("utf-8"), type=RemotePrefillRequest
        )

        sampling_params = request.sampling_params
        sampling_params.max_tokens = 1
        sampling_params.min_tokens = 1

        remote_prefill_params = RemotePrefillParams(
            is_remote_decode=True,
            decode_block_ids=request.block_ids,
            decode_engine_id=request.engine_id,
        )

        # TODO check if metadata has changed
        # and reload - currently only loading once

        vllm_logger.info(
            "Got prefill request from decode engine %s", request.engine_id
        )
        if request.engine_id not in self._loaded_metadata:
            vllm_logger.info(
                "Loading nixl metadata from engine %s", request.engine_id
            )
            remote_metadata = await self._metadata_store.get(request.engine_id)
            vllm_logger.info(
                "Got nixl metadata from store for engine %s", request.engine_id
            )
            await self.engine_client.add_remote_nixl_metadata(remote_metadata)
            vllm_logger.info(
                "Loaded nixl metadata from engine %s into engine %s",
                request.engine_id,
                self.engine_client.nixl_metadata.engine_id,
            )
            self._loaded_metadata.add(request.engine_id)
            vllm_logger.info(
                "Added to _loaded_metadata: %s", request.engine_id
            )
        else:
            vllm_logger.info(
                "Nixl metadata already loaded for engine %s", request.engine_id
            )

        vllm_logger.info(
            "Generating prefill for engine %s", request.engine_id
        )
        async for _ in self.engine_client.generate(
            request_id=request.request_id,
            prompt=TokensPrompt(prompt_token_ids=request.prompt_token_ids),
            sampling_params=sampling_params,
            remote_prefill_params=remote_prefill_params,
        ):
            vllm_logger.info(
                "Generated prefill for engine %s", request.engine_id
            )
            yield


@dynemo_worker()
async def worker(runtime: DistributedRuntime, engine_args: AsyncEngineArgs):
    component = runtime.namespace("test-nixl").component("prefill")
    await component.create_service()

    endpoint = component.endpoint("generate")

    vllm_logger.info("Building engine client")
    async with build_async_engine_client_from_engine_args(engine_args) as engine_client:
        vllm_logger.info("Engine client built")
        metadata = engine_client.nixl_metadata
        metadata_store = NixlMetadataStore("test-nixl", runtime)
        vllm_logger.info("Storing metadata")
        await metadata_store.put(metadata.engine_id, metadata)
        vllm_logger.info("Metadata stored")
        request_handler = RequestHandler(engine_client, metadata_store)
        vllm_logger.info("Serving endpoint")
        await endpoint.serve_endpoint(request_handler.generate)


if __name__ == "__main__":
    uvloop.install()
    engine_args = parse_vllm_args()

    if engine_args.enable_chunked_prefill is not False:
        print("Chunked prefill is not supported yet, setting to False")
        engine_args.enable_chunked_prefill = False

    if engine_args.pipeline_parallel_size != 1:
        print("Pipeline parallel size is not supported yet, setting to 1")
        engine_args.pipeline_parallel_size = 1

    if engine_args.disable_async_output_proc is not True:
        print("Async output processing is not supported yet, setting to True")
        engine_args.disable_async_output_proc = True

    if engine_args.enforce_eager is not True:
        print("Prefill must be done eagerly, setting to True")
        engine_args.enforce_eager = True

    asyncio.run(worker(engine_args))
