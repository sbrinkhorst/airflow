#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import datetime
import json
import time
import warnings
from typing import TYPE_CHECKING, Any, Sequence, cast

from sqlalchemy import select
from sqlalchemy.orm.exc import NoResultFound

from airflow.api.common.trigger_dag import trigger_dag
from airflow.configuration import conf
from airflow.exceptions import (
    AirflowException,
    AirflowSkipException,
    DagNotFound,
    DagRunAlreadyExists,
    RemovedInAirflow3Warning,
)
from airflow.models.baseoperator import BaseOperator
from airflow.models.baseoperatorlink import BaseOperatorLink
from airflow.models.dag import DagModel
from airflow.models.dagbag import DagBag
from airflow.models.dagrun import DagRun
from airflow.models.xcom import XCom
from airflow.triggers.external_task import DagStateTrigger
from airflow.utils import timezone
from airflow.utils.helpers import build_airflow_url_with_query
from airflow.utils.session import provide_session
from airflow.utils.state import DagRunState
from airflow.utils.types import DagRunType

XCOM_LOGICAL_DATE_ISO = "trigger_logical_date_iso"
XCOM_RUN_ID = "trigger_run_id"


if TYPE_CHECKING:
    from sqlalchemy.orm.session import Session

    from airflow.models.taskinstancekey import TaskInstanceKey
    from airflow.utils.context import Context


class TriggerDagRunLink(BaseOperatorLink):
    """
    Operator link for TriggerDagRunOperator.

    It allows users to access DAG triggered by task using TriggerDagRunOperator.
    """

    name = "Triggered DAG"

    def get_link(self, operator: BaseOperator, *, ti_key: TaskInstanceKey) -> str:
        # Fetch the correct dag_run_id for the triggerED dag which is
        # stored in xcom during execution of the triggerING task.
        triggered_dag_run_id = XCom.get_value(ti_key=ti_key, key=XCOM_RUN_ID)
        query = {
            "dag_id": cast(TriggerDagRunOperator, operator).trigger_dag_id,
            "dag_run_id": triggered_dag_run_id,
            "execution_date": XCom.get_value(ti_key=ti_key, key=XCOM_LOGICAL_DATE_ISO),
        }
        return build_airflow_url_with_query(query)


class TriggerDagRunOperator(BaseOperator):
    """
    Triggers a DAG run for a specified DAG ID.

    :param trigger_dag_id: The ``dag_id`` of the DAG to trigger (templated).
    :param trigger_run_id: The run ID to use for the triggered DAG run (templated).
        If not provided, a run ID will be automatically generated.
    :param conf: Configuration for the DAG run (templated).
    :param logical_date: Logical date for the triggered DAG (templated).
    :param reset_dag_run: Whether clear existing DAG run if already exists.
        This is useful when backfill or rerun an existing DAG run.
        This only resets (not recreates) the DAG run.
        DAG run conf is immutable and will not be reset on rerun of an existing DAG run.
        When reset_dag_run=False and dag run exists, DagRunAlreadyExists will be raised.
        When reset_dag_run=True and dag run exists, existing DAG run will be cleared to rerun.
    :param wait_for_completion: Whether or not wait for DAG run completion. (default: False)
    :param poke_interval: Poke interval to check DAG run status when wait_for_completion=True.
        (default: 60)
    :param allowed_states: Optional list of allowed DAG run states of the triggered DAG. This is useful when
        setting ``wait_for_completion`` to True. Must be a valid DagRunState.
        Default is ``[DagRunState.SUCCESS]``.
    :param failed_states: Optional list of failed or disallowed DAG run states of the triggered DAG. This is
        useful when setting ``wait_for_completion`` to True. Must be a valid DagRunState.
        Default is ``[DagRunState.FAILED]``.
    :param skip_when_already_exists: Set to true to mark the task as SKIPPED if a DAG run of the triggered
        DAG for the same logical date already exists.
    :param deferrable: If waiting for completion, whether or not to defer the task until done,
        default is ``False``.
    :param execution_date: Deprecated parameter; same as ``logical_date``.
    """

    template_fields: Sequence[str] = (
        "trigger_dag_id",
        "trigger_run_id",
        "logical_date",
        "conf",
        "wait_for_completion",
        "skip_when_already_exists",
    )
    template_fields_renderers = {"conf": "py"}
    ui_color = "#ffefeb"
    operator_extra_links = [TriggerDagRunLink()]

    def __init__(
        self,
        *,
        trigger_dag_id: str,
        trigger_run_id: str | None = None,
        conf: dict | None = None,
        logical_date: str | datetime.datetime | None = None,
        reset_dag_run: bool = False,
        wait_for_completion: bool = False,
        poke_interval: int = 60,
        allowed_states: list[str | DagRunState] | None = None,
        failed_states: list[str | DagRunState] | None = None,
        skip_when_already_exists: bool = False,
        deferrable: bool = conf.getboolean("operators", "default_deferrable", fallback=False),
        execution_date: str | datetime.datetime | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.trigger_dag_id = trigger_dag_id
        self.trigger_run_id = trigger_run_id
        self.conf = conf
        self.reset_dag_run = reset_dag_run
        self.wait_for_completion = wait_for_completion
        self.poke_interval = poke_interval
        if allowed_states:
            self.allowed_states = [DagRunState(s) for s in allowed_states]
        else:
            self.allowed_states = [DagRunState.SUCCESS]
        if failed_states:
            self.failed_states = [DagRunState(s) for s in failed_states]
        else:
            self.failed_states = [DagRunState.FAILED]
        self.skip_when_already_exists = skip_when_already_exists
        self._defer = deferrable

        if execution_date is not None:
            warnings.warn(
                "Parameter 'execution_date' is deprecated. Use 'logical_date' instead.",
                RemovedInAirflow3Warning,
                stacklevel=2,
            )
            logical_date = execution_date

        if logical_date is not None and not isinstance(logical_date, (str, datetime.datetime)):
            type_name = type(logical_date).__name__
            raise TypeError(
                f"Expected str or datetime.datetime type for parameter 'logical_date'. Got {type_name}"
            )

        self.logical_date = logical_date

    def execute(self, context: Context):
        if isinstance(self.logical_date, datetime.datetime):
            parsed_logical_date = self.logical_date
        elif isinstance(self.logical_date, str):
            parsed_logical_date = timezone.parse(self.logical_date)
        else:
            parsed_logical_date = timezone.utcnow()

        try:
            json.dumps(self.conf)
        except TypeError:
            raise AirflowException("conf parameter should be JSON Serializable")

        if self.trigger_run_id:
            run_id = str(self.trigger_run_id)
        else:
            run_id = DagRun.generate_run_id(DagRunType.MANUAL, parsed_logical_date)

        try:
            dag_run = trigger_dag(
                dag_id=self.trigger_dag_id,
                run_id=run_id,
                conf=self.conf,
                execution_date=parsed_logical_date,
                replace_microseconds=False,
            )

        except DagRunAlreadyExists as e:
            if self.reset_dag_run:
                dag_run = e.dag_run
                self.log.info("Clearing %s on %s", self.trigger_dag_id, dag_run.logical_date)

                # Get target dag object and call clear()
                dag_model = DagModel.get_current(self.trigger_dag_id)
                if dag_model is None:
                    raise DagNotFound(f"Dag id {self.trigger_dag_id} not found in DagModel")

                dag_bag = DagBag(dag_folder=dag_model.fileloc, read_dags_from_db=True)
                dag = dag_bag.get_dag(self.trigger_dag_id)
                dag.clear(start_date=dag_run.logical_date, end_date=dag_run.logical_date)
            else:
                if self.skip_when_already_exists:
                    raise AirflowSkipException(
                        "Skipping due to skip_when_already_exists is set to True and DagRunAlreadyExists"
                    )
                raise e
        if dag_run is None:
            raise RuntimeError("The dag_run should be set here!")
        # Store the run id from the dag run (either created or found above) to
        # be used when creating the extra link on the webserver.
        # TODO: Logical date as xcom stored only for backwards compatibility. Remove in Airflow 3.0
        ti = context["task_instance"]
        ti.xcom_push(key=XCOM_LOGICAL_DATE_ISO, value=dag_run.logical_date.isoformat())
        ti.xcom_push(key=XCOM_RUN_ID, value=dag_run.run_id)

        if self.wait_for_completion:
            # Kick off the deferral process
            if self._defer:
                self.defer(
                    trigger=DagStateTrigger(
                        dag_id=self.trigger_dag_id,
                        states=self.allowed_states + self.failed_states,
                        execution_dates=[dag_run.logical_date],
                        poll_interval=self.poke_interval,
                    ),
                    method_name="execute_complete",
                )
            # wait for dag to complete
            while True:
                self.log.info(
                    "Waiting for %s on %s to become allowed state %s ...",
                    self.trigger_dag_id,
                    dag_run.logical_date,
                    self.allowed_states,
                )
                time.sleep(self.poke_interval)

                dag_run.refresh_from_db()
                state = dag_run.state
                if state in self.failed_states:
                    raise AirflowException(f"{self.trigger_dag_id} failed with failed states {state}")
                if state in self.allowed_states:
                    self.log.info("%s finished with allowed state %s", self.trigger_dag_id, state)
                    return

    @provide_session
    def execute_complete(self, context: Context, session: Session, event: tuple[str, dict[str, Any]]):
        # This logical_date is parsed from the return trigger event
        provided_logical_date = event[1]["execution_dates"][0]
        try:
            dag_run = session.execute(
                select(DagRun).where(
                    DagRun.dag_id == self.trigger_dag_id, DagRun.execution_date == provided_logical_date
                )
            ).scalar_one()
        except NoResultFound:
            raise AirflowException(
                f"No DAG run found for DAG {self.trigger_dag_id} and logical date {self.logical_date}"
            )

        state = dag_run.state

        if state in self.failed_states:
            raise AirflowException(f"{self.trigger_dag_id} failed with failed state {state}")
        if state in self.allowed_states:
            self.log.info("%s finished with allowed state %s", self.trigger_dag_id, state)
            return

        raise AirflowException(
            f"{self.trigger_dag_id} return {state} which is not in {self.failed_states}"
            f" or {self.allowed_states}"
        )
