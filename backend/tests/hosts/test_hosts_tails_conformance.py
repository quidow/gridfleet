from app.hosts.protocols import AgentLogsProtocol, HostEventsProtocol
from app.hosts.service_agent_logs import AgentLogsService
from app.hosts.service_host_events import HostEventsService


def test_hosts_tail_services_satisfy_protocols() -> None:
    assert isinstance(AgentLogsService.__new__(AgentLogsService), AgentLogsProtocol)
    assert isinstance(HostEventsService.__new__(HostEventsService), HostEventsProtocol)
