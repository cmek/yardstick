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

"""A runner that every run changes a specified input value to the scenario.
The input value in the sequence is specified in a list in the input file.
"""

from __future__ import absolute_import

import logging
import multiprocessing
import time
import traceback

import os

from yardstick.benchmark.runners import base
from yardstick.common import exceptions as y_exc

LOG = logging.getLogger(__name__)


def _worker_process(queue, cls, method_name, scenario_cfg,
                    context_cfg, aborted, output_queue):

    runner_cfg = scenario_cfg['runner']

    interval = runner_cfg.get("interval", 1)
    arg_name = runner_cfg.get('scenario_option_name')
    sequence_values = runner_cfg.get('sequence')

    if 'options' not in scenario_cfg:
        scenario_cfg['options'] = {}

    options = scenario_cfg['options']

    runner_cfg['runner_id'] = os.getpid()

    LOG.info("worker START, sequence_values(%s, %s), class %s",
             arg_name, sequence_values, cls)

    scenario_output = base.ScenarioOutput(queue, sequence=1, errors="")
    benchmark = cls(scenario_cfg, context_cfg)
    benchmark.setup()
    method = getattr(benchmark, method_name)

    sla_action = None
    if "sla" in scenario_cfg:
        sla_action = scenario_cfg["sla"].get("action", "assert")

    for value in sequence_values:
        options[arg_name] = value

        LOG.debug("runner=%(runner)s seq=%(sequence)s START",
                  {"runner": runner_cfg["runner_id"],
                   "sequence": scenario_output.sequence})

        scenario_output.clear()
        scenario_output.errors = ""

        try:
            result = method(scenario_output)
        except y_exc.SLAValidationError as error:
            # SLA validation failed in scenario, determine what to do now
            if sla_action == "assert":
                raise
            elif sla_action == "monitor":
                LOG.warning("SLA validation failed: %s", error.args)
                scenario_output.errors = error.args
        except Exception as e:  # pylint: disable=broad-except
            scenario_output.errors = traceback.format_exc()
            LOG.exception(e)
        else:
            if result:
                output_queue.put(result)

        time.sleep(interval)

        if scenario_output:
            scenario_output.push()

        LOG.debug("runner=%(runner)s seq=%(sequence)s END",
                  {"runner": runner_cfg["runner_id"],
                   "sequence": scenario_output.sequence})

        scenario_output.sequence += 1

        if (scenario_output.errors and sla_action is None) or aborted.is_set():
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
    LOG.info("worker END")
    LOG.debug("queue.qsize() = %s", queue.qsize())
    LOG.debug("output_queue.qsize() = %s", output_queue.qsize())


class SequenceRunner(base.Runner):
    """Run a scenario by changing an input value defined in a list

  Parameters
    interval - time to wait between each scenario invocation
        type:    int
        unit:    seconds
        default: 1 sec
    scenario_option_name - name of the option that is increased each invocation
        type:    string
        unit:    na
        default: none
    sequence - list of values which are executed in their respective scenarios
        type:    [int]
        unit:    na
        default: none
    """

    __execution_type__ = 'Sequence'

    def _run_benchmark(self, cls, method, scenario_cfg, context_cfg):
        name = "{}-{}-{}".format(self.__execution_type__, scenario_cfg.get("type"), os.getpid())
        self.process = multiprocessing.Process(
            name=name,
            target=_worker_process,
            args=(self.result_queue, cls, method, scenario_cfg,
                  context_cfg, self.aborted, self.output_queue))
        self.process.start()
