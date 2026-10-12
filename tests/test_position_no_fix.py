"""Meshtastic Position packets with no GPS fix must not become map points.

A node without a fix sends Position with latitude_i = longitude_i = 0.
Decoding that as 0.0 / 0.0 stored the node at (0, 0) ("Null Island"), so
every no-fix node stacked on one marker, and COALESCE in the upsert let a
later (0, 0) overwrite a real stored position.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from src.decode.meshtastic_decoder import MeshtasticDecoder
from src.decode.portnum_handlers import _decode_position
from src.models.node import Node
from src.models.packet import Packet, PacketType, Protocol
from src.storage.database import DatabaseManager
from src.storage.node_repository import NodeRepository

mesh_pb2 = pytest.importorskip("meshtastic.protobuf.mesh_pb2")


def position_bytes(lat_i: int, lon_i: int, **kw) -> bytes:
    pos = mesh_pb2.Position()
    pos.latitude_i = lat_i
    pos.longitude_i = lon_i
    for k, v in kw.items():
        setattr(pos, k, v)
    return pos.SerializeToString()


def test_no_fix_position_has_no_coordinates():
    payload, ptype = _decode_position(position_bytes(0, 0, altitude=120))
    assert ptype == PacketType.POSITION
    assert payload["latitude"] is None
    assert payload["longitude"] is None


def test_real_position_is_unchanged():
    payload, _ = _decode_position(position_bytes(375_000_000, -1_220_000_000))
    assert payload["latitude"] == pytest.approx(37.5)
    assert payload["longitude"] == pytest.approx(-122.0)


def test_equator_or_meridian_alone_is_still_valid():
    payload, _ = _decode_position(position_bytes(0, 1_000_000_000))
    assert payload["latitude"] == 0.0
    assert payload["longitude"] == pytest.approx(100.0)


def test_out_of_range_position_is_dropped():
    payload, _ = _decode_position(position_bytes(950_000_000, 0))  # 95 deg lat
    assert payload["latitude"] is None and payload["longitude"] is None


def make_packet(decoded):
    return Packet(
        packet_id="1", source_id="aabbccdd", destination_id="ffffffff",
        protocol=Protocol.MESHTASTIC, packet_type=PacketType.POSITION,
        decoded_payload=decoded,
    )


def test_node_from_no_fix_position_has_no_position():
    payload, _ = _decode_position(position_bytes(0, 0))
    node = MeshtasticDecoder(mock.Mock()).extract_node_update(make_packet(payload))
    assert node.latitude is None and node.longitude is None


def test_stored_position_survives_a_later_no_fix_report(tmp_path):
    async def run():
        db = DatabaseManager(str(tmp_path / "t.db"))
        await db.connect()
        repo = NodeRepository(db)
        decoder = MeshtasticDecoder(mock.Mock())

        good, _ = _decode_position(position_bytes(375_000_000, -1_220_000_000))
        await repo.upsert(decoder.extract_node_update(make_packet(good)))
        nofix, _ = _decode_position(position_bytes(0, 0))
        await repo.upsert(decoder.extract_node_update(make_packet(nofix)))

        stored = await repo.get_by_id("aabbccdd")
        await db.disconnect()
        return stored

    stored = asyncio.run(run())
    assert stored.latitude == pytest.approx(37.5)
    assert stored.longitude == pytest.approx(-122.0)


def test_existing_null_island_rows_are_not_returned_for_the_map(tmp_path):
    async def run():
        db = DatabaseManager(str(tmp_path / "t.db"))
        await db.connect()
        repo = NodeRepository(db)
        await repo.upsert(Node(node_id="00000001", protocol="meshtastic",
                               latitude=0.0, longitude=0.0))
        await repo.upsert(Node(node_id="00000002", protocol="meshtastic",
                               latitude=40.0, longitude=-75.0))
        rows = await repo.get_with_position()
        await db.disconnect()
        return [n.node_id for n in rows]

    assert asyncio.run(run()) == ["00000002"]


def test_startup_migration_clears_rows_already_stored_at_null_island(tmp_path):
    path = str(tmp_path / "t.db")

    async def run():
        db = DatabaseManager(path)
        await db.connect()
        await db.execute(
            "INSERT INTO nodes (node_id, protocol, latitude, longitude, "
            "last_heard, first_seen, packet_count) VALUES "
            "('a1', 'meshtastic', 0.0, 0.0, '2026-01-01T00:00:00', '2026-01-01T00:00:00', 1), "
            "('b2', 'meshtastic', 40.0, -75.0, '2026-01-01T00:00:00', '2026-01-01T00:00:00', 1)"
        )
        await db.commit()
        await db.disconnect()

        db = DatabaseManager(path)  # reopening runs the migrations
        await db.connect()
        repo = NodeRepository(db)
        a, b = await repo.get_by_id("a1"), await repo.get_by_id("b2")
        await db.disconnect()
        return a, b

    a, b = asyncio.run(run())
    assert a.latitude is None and a.longitude is None
    assert (b.latitude, b.longitude) == (40.0, -75.0)


def test_meshcore_advert_without_fix_does_not_set_position():
    from src.decode.meshcore_decoder import MeshcoreDecoder

    node = Node(node_id="mc:1", protocol="meshcore")
    MeshcoreDecoder._apply_position(node, {"latitude": 0.0, "longitude": 0.0})
    assert node.latitude is None and node.longitude is None
    MeshcoreDecoder._apply_position(node, {"latitude": 51.5, "longitude": -0.12})
    assert (node.latitude, node.longitude) == (51.5, -0.12)
