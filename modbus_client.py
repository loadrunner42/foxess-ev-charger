"""Modbus TCP client for FoxESS EV Charger."""
from __future__ import annotations

import logging
import socket

_LOGGER = logging.getLogger(__name__)

# ── Modbus Function Codes ─────────────────────────────────────────────────────
FC_READ_HOLDING     = 0x03   # Lesen R/W und R-Only Register
FC_WRITE_SINGLE     = 0x06   # Schreiben W-Only Register  (0x4000–0x4003)
FC_WRITE_MULTIPLE   = 0x10   # Schreiben R/W Register     (0x3000–0x300B)

# ── W-Only Register Adressen (FC 0x06) ───────────────────────────────────────
WRITE_ONLY_REGISTERS = {0x4000, 0x4001, 0x4002, 0x4003}


class FoxESSModbusClient:
    """Minimal Modbus TCP client (raw sockets, kein pymodbus)."""

    def __init__(self, host: str, port: int, slave_id: int) -> None:
        self._host      = host
        self._port      = port
        self._slave_id  = slave_id
        self._tid       = 0

    # ── Interne Hilfsmethoden ─────────────────────────────────────────────────

    def _next_tid(self) -> int:
        self._tid = (self._tid + 1) % 0xFFFF
        return self._tid

def _recv_exact(
    self,
    sock: socket.socket,
    length: int,
) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError(
                "Modbus TCP connection closed before full response was received"
            )
        data.extend(chunk)
    return bytes(data)


def _send_recv(
    self,
    request: bytes,
    timeout: float = 5.0,
) -> bytes | None:
    try:
        with socket.create_connection(
            (self._host, self._port),
            timeout=timeout,
        ) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)

            header = self._recv_exact(sock, 7)

            protocol_id = int.from_bytes(header[2:4], "big")
            response_length = int.from_bytes(header[4:6], "big")

            if protocol_id != 0:
                raise ValueError(
                    f"Invalid Modbus protocol ID: {protocol_id}"
                )

            pdu_length = response_length - 1

            if pdu_length < 1:
                raise ValueError(
                    f"Invalid Modbus response length: {response_length}"
                )

            pdu = self._recv_exact(sock, pdu_length)
            response = header + pdu
            if response[0:2] != request[0:2]:
                raise ValueError(
                    "Modbus transaction ID mismatch: "
                    f"sent {request[0:2].hex()}, "
                    f"received {response[0:2].hex()}"
                )
            if response[6] != self._slave_id:
                raise ValueError(
                    "Modbus unit ID mismatch: "
                    f"expected {self._slave_id}, received {response[6]}"
                )
            return response
    except Exception as ex:
        _LOGGER.error(
            "Modbus TCP %s:%s communication error: %s",
            self._host,
            self._port,
            ex,
        )
        return None

    
    def _build_mbap(self, pdu: bytes) -> bytes:
        """Baut den vollständigen Modbus TCP ADU (MBAP + PDU)."""
        tid     = self._next_tid()
        length  = 1 + len(pdu)   # Unit ID (1) + PDU
        return (
            tid.to_bytes(2, "big")              +  # Transaction ID
            (0).to_bytes(2, "big")              +  # Protocol ID
            length.to_bytes(2, "big")           +  # Length
            self._slave_id.to_bytes(1, "big")   +  # Unit ID
            pdu
        )

    # ── Öffentliche Methoden ──────────────────────────────────────────────────

    def read_registers(self, address: int, count: int) -> list[int] | None:
        """Liest `count` Holding-Register ab `address` (FC 0x03)."""
        pdu = (
            FC_READ_HOLDING.to_bytes(1, "big") +
            address.to_bytes(2, "big")         +
            count.to_bytes(2, "big")
        )
        response = self._send_recv(self._build_mbap(pdu))
        if response is None:
            return None

        # Modbus Exception prüfen
        if len(response) >= 9 and response[7] == (FC_READ_HOLDING | 0x80):
            _LOGGER.error("Modbus FC03 Exception 0x%02X @ 0x%04X", response[8], address)
            return None

        if len(response) < 9:
            _LOGGER.warning("FC03: zu kurze Antwort (%d Bytes)", len(response))
            return None

        byte_count = response[8]
        payload    = response[9: 9 + byte_count]
        registers  = [int.from_bytes(payload[i:i+2], "big") for i in range(0, byte_count, 2)]
        _LOGGER.debug("FC03 Read 0x%04X count=%d → %s", address, count, registers)
        return registers

    def read_uint32(self, address: int) -> int | None:
        """Liest einen UINT32-Wert aus zwei aufeinanderfolgenden Registern."""
        regs = self.read_registers(address, 2)
        if regs and len(regs) >= 2:
            return (regs[0] << 16) | regs[1]
        return None

    def write_holding_register(self, address: int, value: int) -> bool:
        """
        Schreibt ein einzelnes Register.

        R/W Register (0x3000–0x300B) → FC 0x10 (Write Multiple Registers)
        W-Only Register (0x4000–0x4003) → FC 0x06 (Write Single Register)
        """
        if address in WRITE_ONLY_REGISTERS:
            return self._write_single(address, value)
        else:
            return self._write_multiple(address, value)

    def write_holding_registers(
        self,
        address: int,
        values: list[int],
    ) -> bool:
        """Write consecutive R/W holding registers in one FC 0x10 request."""
        if not values:
            return True
    
        if not 1 <= len(values) <= 123:
            raise ValueError(
                f"Modbus FC10 supports 1-123 registers, got {len(values)}"
            )
    
        for value in values:
            if not 0 <= value <= 0xFFFF:
                raise ValueError(
                    f"Register value must be 0-65535, got {value}"
                )
    
        quantity = len(values)
        payload = b"".join(
            value.to_bytes(2, "big")
            for value in values
        )
    
        pdu = (
            FC_WRITE_MULTIPLE.to_bytes(1, "big")
            + address.to_bytes(2, "big")
            + quantity.to_bytes(2, "big")
            + len(payload).to_bytes(1, "big")
            + payload
        )
    
        response = self._send_recv(self._build_mbap(pdu))
    
        if response is None:
            return False
    
        if len(response) >= 9 and response[7] == (
            FC_WRITE_MULTIPLE | 0x80
        ):
            _LOGGER.error(
                "FC10 block-write exception 0x%02X @ 0x%04X count=%d",
                response[8],
                address,
                quantity,
            )
            return False
    
        if len(response) < 12:
            _LOGGER.warning(
                "FC10 block write 0x%04X count=%d: short response: %s",
                address,
                quantity,
                response.hex(),
            )
            return False
    
        response_address = int.from_bytes(response[8:10], "big")
        response_quantity = int.from_bytes(response[10:12], "big")
    
        success = (
            response_address == address
            and response_quantity == quantity
        )
    
        if success:
            _LOGGER.debug(
                "FC10 block write 0x%04X count=%d values=%s",
                address,
                quantity,
                values,
            )
        else:
            _LOGGER.warning(
                "FC10 block write mismatch: requested 0x%04X/%d, "
                "response 0x%04X/%d",
                address,
                quantity,
                response_address,
                response_quantity,
            )
    
        return success
    
    # ── Private Write-Methoden ────────────────────────────────────────────────

    def _write_single(self, address: int, value: int) -> bool:
        """FC 0x06 – Write Single Register (W-Only Register 0x4000–0x4003)."""
        pdu = (
            FC_WRITE_SINGLE.to_bytes(1, "big") +
            address.to_bytes(2, "big")         +
            value.to_bytes(2, "big")
        )
        response = self._send_recv(self._build_mbap(pdu))
        if response is None:
            return False

        if len(response) >= 9 and response[7] == (FC_WRITE_SINGLE | 0x80):
            _LOGGER.error(
                "FC06 Exception 0x%02X @ 0x%04X value=%d",
                response[8], address, value,
            )
            return False

        success = len(response) >= 12
        if success:
            _LOGGER.debug("FC06 Write 0x%04X = %d ✓", address, value)
        else:
            _LOGGER.warning(
                "FC06 Write 0x%04X = %d: unerwartete Antwort (%d Bytes): %s",
                address, value, len(response), response.hex(),
            )
        return success

    def _write_multiple(self, address: int, value: int) -> bool:
        """FC 0x10 – Write Multiple Registers (R/W Register 0x3000–0x300B)."""
        pdu = (
            FC_WRITE_MULTIPLE.to_bytes(1, "big") +
            address.to_bytes(2, "big")           +
            (1).to_bytes(2, "big")               +  # Quantity = 1 Register
            (2).to_bytes(1, "big")               +  # ByteCount = 2
            value.to_bytes(2, "big")
        )
        response = self._send_recv(self._build_mbap(pdu))
        if response is None:
            return False

        if len(response) >= 9 and response[7] == (FC_WRITE_MULTIPLE | 0x80):
            _LOGGER.error(
                "FC10 Exception 0x%02X @ 0x%04X value=%d",
                response[8], address, value,
            )
            return False

        success = len(response) >= 12
        if success:
            _LOGGER.debug("FC10 Write 0x%04X = %d ✓", address, value)
        else:
            _LOGGER.warning(
                "FC10 Write 0x%04X = %d: unerwartete Antwort (%d Bytes): %s",
                address, value, len(response), response.hex(),
            )
        return success

    def disconnect(self) -> None:
        """Kein persistenter Socket – nichts zu schließen."""
        pass
