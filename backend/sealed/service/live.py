"""Live transport wiring. Clients are created only for an authorized dispatch."""

from sealed.fo.adapter import FoTransport
from sealed.fo.client import LiveFoClient
from sealed.ingest.audit import AuditRequest, AuditStore, GmailAuditor


class LiveTransport:
    def __init__(self, reader, engine, *, state, audits):
        self.reader, self.engine, self.state, self.audits = reader, engine, state, audits

    def send(self, now, msg, approve_preview):
        with AuditStore(self.audits) as store:
            auditor = GmailAuditor(self.reader, self.engine.guard, store)
            def audit(outbound, attempted_at):
                return auditor.wait(AuditRequest(outbound, attempted_at, ("fo@wajo.ai", self.reader.address),
                    thread_id=outbound.reply_thread_id or ""), seconds=30)
            client = LiveFoClient()
            try:
                return FoTransport(client, self.state, sent_auditor=audit).send(now, msg, approve_preview)
            finally:
                client.close()

    def poll_call(self, now, msg):
        from sealed.fo.calls import CallTransport
        client = LiveFoClient()
        try:
            return CallTransport(client, self.state).poll_call(now, msg)
        finally:
            client.close()
