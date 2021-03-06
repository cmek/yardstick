# Copyright 2014: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# yardstick comment: this is a modified copy of
# rally/rally/benchmark/runners/constant.py

"""A runner that runs a specific time before it returns
"""

from __future__ import absolute_import

import os
import multiprocessing
import logging
import traceback
import time

from yardstick.benchmark.runners import base
from yardstick.common import exceptions as y_exc
from yardstick.common import constants

LOG = logging.getLogger(__name__)

def _worker_process(queue, cls, method_name, scenario_cfg,
                    context_cfg, aborted, output_queue):

    sequence = 1

    runner_cfg = scenario_cfg['runner']

    requested_interval = interval = runner_cfg.get("interval", 1)
    duration = runner_cfg.get("duration", 60)
    sampled = runner_cfg.get("sampled", False)

    LOG.info("Worker START, duration is %ds", duration)
    LOG.debug("class is %s", cls)

    runner_cfg['runner_id'] = os.getpid()

    benchmark = cls(scenario_cfg, context_cfg)
    benchmark.setup()
    method = getattr(benchmark, method_name)

    sla_action = None
    if "sla" in scenario_cfg:
        sla_action = scenario_cfg["sla"].get("action", "assert")


    start = time.time()
    timeout = start + duration
    while True:

        LOG.debug("runner=%(runner)s seq=%(sequence)s START",
                  {"runner": runner_cfg["runner_id"], "sequence": sequence})

        data = {}
        errors = ""

        benchmark.pre_run_wait_time(interval)

        if sampled:
            try:
                pre_adjustment = time.time()
                result = method(data)
                post_adjustment = time.time()
                if requested_interval > post_adjustment - pre_adjustment:
                    interval = requested_interval - (post_adjustment - pre_adjustment)
                else:
                    interval = 0

            except y_exc.SLAValidationError as error:
                # SLA validation failed in scenario, determine what to do now
                if sla_action == "assert":
                    raise
                elif sla_action == "monitor":
                    LOG.warning("SLA validation failed: %s", error.args)
                    errors = error.args
            # catch all exceptions because with multiprocessing we can have un-picklable exception
            # problems  https://bugs.python.org/issue9400
            except Exception:  # pylint: disable=broad-except
                errors = traceback.format_exc()
                LOG.exception("")
            else:
                if result:
                    # add timeout for put so we don't block test
                    # if we do timeout we don't care about dropping individual KPIs
                    output_queue.put(result, True, constants.QUEUE_PUT_TIMEOUT)

            benchmark_output = {
                'timestamp': time.time(),
                'sequence': sequence,
                'data': data,
                'errors': errors
            }

            queue.put(benchmark_output, True, constants.QUEUE_PUT_TIMEOUT)
        else:
            LOG.debug("No sample collected ...Sequence %s", sequence)


        sequence += 1

        if ((errors and sla_action is None) or time.time() > timeout
                or aborted.is_set() or benchmark.is_ended()):
            LOG.info("Worker END")
            break

    try:
        benchmark.teardown()
    except Exception:
        # catch any exception in teardown and convert to simple exception
        # never pass exceptions back to multiprocessing, because some exceptions can
        # be unpicklable
        # https://bugs.python.org/issue9400
        LOG.exception("")
        raise SystemExit(1)

    LOG.debug("queue.qsize() = %s", queue.qsize())
    LOG.debug("output_queue.qsize() = %s", output_queue.qsize())
    LOG.info("Exiting ProxDuration Runner...")

class ProxDurationRunner(base.Runner):
    """Run a scenario for a certain amount of time

If the scenario ends before the time has elapsed, it will be started again.

  Parameters
    duration - amount of time the scenario will be run for
        type:    int
        unit:    seconds
        default: 60 sec
    interval - time to wait between each scenario invocation
        type:    int
        unit:    seconds
        default: 1 sec
    sampled - Sample data is required yes/no
        type:    boolean
        unit:    True/False
        default: False
    confirmation - Number of confirmation retries
        type:    int
        unit:    retry attempts
        default: 0
    """
    __execution_type__ = 'ProxDuration'

    def _run_benchmark(self, cls, method, scenario_cfg, context_cfg):
        name = "{}-{}-{}".format(self.__execution_type__, scenario_cfg.get("type"), os.getpid())
        self.process = multiprocessing.Process(
            name=name,
            target=_worker_process,
            args=(self.result_queue, cls, method, scenario_cfg,
                  context_cfg, self.aborted, self.output_queue))
        self.process.start()
