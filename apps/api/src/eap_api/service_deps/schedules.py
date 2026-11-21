"""Provider of the schedules service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import ScheduleStoreDep, SessionDep
from eap_api.services.schedules import ScheduleService


def get_schedule_service(session: SessionDep, store: ScheduleStoreDep) -> ScheduleService:
    return ScheduleService(session, store)


ScheduleServiceDep = Annotated[ScheduleService, Depends(get_schedule_service)]
