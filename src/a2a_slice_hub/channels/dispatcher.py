"""Destination-isolated at-least-once delivery and shared approval projections."""
import json
import httpx
from typing import Protocol

from ..errors import Conflict
from ..store import strict_integer


class ChannelAdapter(Protocol):
    provider: str

    def send(self, event_id: str, topic: str, payload: dict): ...
    def update_card(self, projection: dict, status: str, approved_by: str): ...


class MultiOutboxDispatcher:
    def __init__(self, store, adapters: dict[str, ChannelAdapter], max_tries=5):
        strict_integer(max_tries, 1)
        self.store, self.adapters, self.max_tries = store, dict(adapters), max_tries
        for destination_id, adapter in self.adapters.items():
            store.register_destination(destination_id, adapter.provider)

    def _projection(self, approval_id, destination_id):
        return next((p for p in self.store.get_card_projections(approval_id)
                     if p["destination_id"] == destination_id), None)

    def _approved_by(self, approval_id):
        with self.store.connect() as conn:
            return self.store.one(conn, "approvals", approval_id)["approved_by"]

    def _sync(self, adapter, projection, actor):
        if projection and projection["rendered_version"] < 2:
            adapter.update_card(projection, "APPROVED", actor)
            self.store.record_card_projection(projection["approval_id"], projection["destination_id"],
                adapter.provider, projection["provider_card_id"], rendered_version=2, status="APPROVED", approved_by=actor)

    def _dispatch(self, destination_id, row):
        adapter = self.adapters[destination_id]
        payload = json.loads(row["payload"])
        if "item" in payload:
            item = self.store.get_item(payload["item"])
            payload = {"slice_id": item["slice_id"], "owner": item["owner"], **payload}
        if row["topic"] == "outbox.destination_replayed":
            return None  # Durable control-plane audit, not a new IM notification.
        if row["topic"] == "approval.requested":
            approval_id = payload["card"]["approval_id"]
            actor = self._approved_by(approval_id)
            projection = self._projection(approval_id, destination_id)
            if not projection:
                payload = payload | {"status": "APPROVED" if actor else "PENDING",
                                     "approved_by": actor, "rendered_version": 2 if actor else 1}
                receipt = adapter.send(row["id"], row["topic"], payload)
                if not isinstance(receipt, str) or not receipt:
                    raise ValueError("provider card receipt required")
                self.store.record_card_projection(approval_id, destination_id, adapter.provider, receipt,
                    rendered_version=payload["rendered_version"], status=payload["status"], approved_by=actor)
                projection = self._projection(approval_id, destination_id)
            # Approval can commit while the initial HTTP request is in flight, even after
            # another dispatcher has processed acceptance with no projection yet.
            actor = self._approved_by(approval_id)
            if actor:
                self._sync(adapter, projection, actor)
            return projection["provider_card_id"]
        if row["topic"] == "approval.accepted":
            projection = self._projection(payload["approval_id"], destination_id)
            self._sync(adapter, projection, payload["approved_by"])
            return projection["provider_card_id"] if projection else None
        return adapter.send(row["id"], row["topic"], payload)

    def once(self, destination_id=None):
        destinations = [destination_id] if destination_id is not None else list(self.adapters)
        processed = False
        for destination in destinations:
            if destination not in self.adapters:
                raise ValueError("unknown destination")
            row = self.store.claim_delivery(destination, max_tries=self.max_tries)
            if row is None:
                continue
            processed = True
            try:
                receipt = self._dispatch(destination, row)
            except Exception as exc:
                error = type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    error += ": HTTP " + str(exc.response.status_code)
                success, receipt = False, None
            else:
                success, error = True, None
            try:
                self.store.ack_delivery(row["id"], destination, row["claim"], success,
                    error=error, provider_receipt=receipt, max_tries=self.max_tries)
            except Conflict:
                # A slow send may outlive its reservation. Its stale ACK must not
                # abort other destinations or overwrite a newer claimant's state.
                pass
        return processed
