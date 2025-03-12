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
import enum
import random
import threading
import traceback
import weakref
from queue import Queue
from typing import AsyncIterator, Callable, Optional, Union

from common.protocol import Tokens
from tensorrt_llm.logger import logger

from dynamo.llm import AggregatedMetrics, KvIndexer, KvMetricsAggregator, OverlapScores
from dynamo.runtime import dynamo_endpoint

logger.set_level("info")


class RoutingStrategy(enum.Enum):
    ROUND_ROBIN = "round_robin"
    RANDOM = "random"
    PREFIX = "prefix"


class Scheduler:
    def __init__(
        self, indexer: KvIndexer, metrics_aggregator: KvMetricsAggregator, client
    ):
        self.indexer = indexer
        self.metrics_aggregator = metrics_aggregator
        self.workers_client = client

    def _cost_function(
        self,
        scores: OverlapScores | None,
        metrics: AggregatedMetrics | None,
        token_length: int,
    ):
        worker_scores = {}
        if scores:
            for worker_id, score in scores.scores.items():
                # score is number of matching blocks we multiply by block_size to get tokens
                # and compare to token_length. The larger the cache hit the better
                worker_scores[worker_id] = (
                    score * self.indexer.block_size() / token_length
                )

        worker_metrics = {}
        # pull metrics for each worker
        max_waiting = 0.0
        if metrics:
            for endpoint in metrics.endpoints:
                worker_id = endpoint.worker_id
            worker_metrics[worker_id] = {
                "gpu_cache_usage_perc": endpoint.gpu_cache_usage_perc
                if hasattr(endpoint, "gpu_cache_usage_perc")
                else 0.0,
                "num_requests_waiting": endpoint.num_requests_waiting
                if hasattr(endpoint, "num_requests_waiting")
                else 0.0,
                "gpu_prefix_cache_hit_rate": endpoint.gpu_prefix_cache_hit_rate
                if hasattr(endpoint, "gpu_prefix_cache_hit_rate")
                else 0.0,
            }
            max_waiting = max(
                max_waiting, worker_metrics[worker_id]["num_requests_waiting"]
            )

        # Get all worker IDs from the client. This is needed because scores / metrics may not have values for all workers
        # and we want all workers to be considered in the logit calculation
        worker_ids = self.workers_client.endpoint_ids()

        worker_logits = {}
        for worker_id in worker_ids:
            # Use default values if worker not in scores or metrics
            score = worker_scores.get(worker_id, 0.0)
            metrics_dict = worker_metrics.get(
                worker_id,
                {
                    "gpu_cache_usage_perc": 0.0,
                    "num_requests_waiting": 0.0,
                    "gpu_prefix_cache_hit_rate": 0.0,
                },
            )

            normalized_waiting = (
                metrics_dict["num_requests_waiting"] / max_waiting
                if max_waiting > 0
                else 0.0
            )

            # Have 1 metric that weights towards cache hit
            # 2 metrics that penalize overloaded worker and queuing
            worker_logits[worker_id] = (
                2 * score - metrics_dict["gpu_cache_usage_perc"] - normalized_waiting
            )
            logger.info(
                f"Formula for {worker_id}: {worker_logits[worker_id]:.3f} = 2.0 * {score:.3f} - {metrics_dict['gpu_cache_usage_perc']:.3f} - {normalized_waiting:.3f}"
            )

        if not worker_logits or all(logit == 0 for logit in worker_logits.values()):
            return ""

        # Select the worker with the highest logit
        if worker_logits:
            max_logit = max(worker_logits.values())
            best_workers = [
                wid for wid, logit in worker_logits.items() if logit == max_logit
            ]
            best_worker_id = random.choice(best_workers)
        else:
            best_worker_id = ""

        # Log the metrics for the selected worker
        if best_worker_id:
            logger.info(
                f"Selected worker: {best_worker_id}, logit: {worker_logits[best_worker_id]:.3f}"
            )
            logger.info(
                f"Score: {scores.scores.get(best_worker_id, 0.0) if scores else 0.0:.3f}"
            )

            metrics_dict = worker_metrics.get(best_worker_id, {})
            logger.info(
                f"GPU Cache Hit Rate: {metrics_dict.get('gpu_prefix_cache_hit_rate', 0.0):.3f}"
            )
            logger.info(
                f"GPU Cache Usage: {metrics_dict.get('gpu_cache_usage_perc', 0.0):.3f}"
            )
            logger.info(
                f"Requests Waiting: {metrics_dict.get('num_requests_waiting', 0.0) / max_waiting if max_waiting > 0 else 0.0:.3f}"
            )

        return best_worker_id, worker_scores.get(best_worker_id, 0.0)

    @dynamo_endpoint(Tokens, str)
    async def generate(self, request) -> AsyncIterator[str]:
        if self.indexer is None or self.metrics_aggregator is None:
            yield "_0.0"

        lora_id = 0
        worker_id = ""
        try:
            scores = await self.indexer.find_matches_for_request(
                request.tokens, lora_id
            )
        except Exception:
            scores = {}
            logger.exception(f"Error during worker selection: {traceback.format_exc()}")

        token_length = len(request.tokens)
        metrics = await self.metrics_aggregator.get_metrics()
        schedule_result = self._cost_function(scores, metrics, token_length)
        if schedule_result == "":
            worker_id = ""
            prefix_hit_rate = 0.0
        else:
            worker_id, prefix_hit_rate = schedule_result

        yield f"{worker_id}_{prefix_hit_rate}"


async def get_worker_id(scheduler: Scheduler, request, tokenizer) -> str:
    # NOTE: this will increase TTFT since we are encoding the prompt here
    # prompt is also encoded in the worker.
    # TODO: we need to implement our own request processing and protocols to send only token ids to llmapi worker.
    token_ids = tokenizer.encode(request.prompt)
    worker_id_generator: AsyncIterator = scheduler.generate(
        Tokens(tokens=token_ids).model_dump_json()
    )

    response = await worker_id_generator.__anext__()  # only one worker id is returned
    worker_id, prefix_hit_rate = response.data().split("_")
    prefix_hit_rate = float(prefix_hit_rate)

    logger.debug(
        f"Scheduling to worker_id: {worker_id} with estimated prefix hit rate: {prefix_hit_rate}"
    )
    return worker_id


class ManagedThread(threading.Thread):
    def __init__(
        self,
        task: Optional[Union[Callable[..., bool], weakref.WeakMethod]],
        error_queue: Optional[Queue] = None,
        name: Optional[str] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        **kwargs,
    ):
        super().__init__(name=name)
        self.task = task
        self.error_queue = error_queue
        self.kwargs = kwargs
        self.loop = loop
        self.daemon = True

        self.stop_event = threading.Event()

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def run(self):
        while not self.stop_event.is_set():
            task: Optional[Union[Callable[..., bool], weakref.WeakMethod]] = self.task
            if isinstance(task, weakref.WeakMethod):
                task = task()
                if task is None:
                    # Normally, this should not happen.
                    logger.warning("WeakMethod is expired.")
                    break

            if task is None:
                break

            try:
                if self.loop is None:
                    logger.error("[ManagedThread] Loop not initialized!")
                    break
                future = asyncio.run_coroutine_threadsafe(
                    task(**self.kwargs), self.loop
                )
                _ = future.result()
            except Exception as e:
                logger.error(
                    f"Error in thread {self.name}: {e}\n{traceback.format_exc()}"
                )
                if self.error_queue is not None:
                    self.error_queue.put(e)

        logger.info(f"Thread {self.name} stopped.")

    def stop(self):
        self.stop_event.set()


async def wait_for_workers(client, min_workers: int):
    wait_task = client.wait_for_endpoints()
    await asyncio.sleep(1)

    while not wait_task.done():
        logger.info("Waiting for workers to be ready...")
        await asyncio.sleep(5)

    while len(client.endpoint_ids()) < min_workers:
        logger.info(
            f"Waiting for more workers... Current: {len(client.endpoint_ids())}, Required: {min_workers}"
        )
        await asyncio.sleep(5)

    logger.info(
        f"Required number of workers ({min_workers}) are ready:\n"
        + "\n".join(f"id: {id}" for id in client.endpoint_ids())
    )
