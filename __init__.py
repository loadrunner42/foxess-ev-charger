"""FoxESS EV Charger integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, PLATFORMS,
    CONF_HOST, CONF_PORT, CONF_SLAVE_ID,
    DEFAULT_SCAN_INTERVAL,
    REG_WORK_MODE,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    REG_ALLOWED_CHARGE_TIME,
    REG_ALLOWED_CHARGE_ENERGY,
    REG_TIME_VALIDITY,
    REG_DEFAULT_CURRENT,
    REG_AUTO_PHASE_SWITCH,
    REG_MIN_SWITCH_INTERVAL,
)
from .modbus_client import FoxESSModbusClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host      = entry.data[CONF_HOST]
    port      = entry.data[CONF_PORT]
    slave_id  = entry.data[CONF_SLAVE_ID]
    scan_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)

    client      = FoxESSModbusClient(host, port, slave_id)
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval, entry.entry_id)
    
    await coordinator.async_load_command_cache()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "client":      client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await hass.async_add_executor_job(data["client"].disconnect)
    return unload_ok


class FoxESSChargerCoordinator(DataUpdateCoordinator):
    """Coordinator: pollt alle Modbus-Register des Chargers."""

    _REGISTER_TO_DATA_KEY = {
        REG_WORK_MODE: "work_mode",
        REG_MAX_CHARGING_CURRENT: "max_charging_current_raw",
        REG_MAX_CHARGING_POWER: "max_charging_power_raw",
        REG_ALLOWED_CHARGE_TIME: "allowed_charge_time",
        REG_ALLOWED_CHARGE_ENERGY: "allowed_charge_energy",
        REG_TIME_VALIDITY: "time_validity",
        REG_DEFAULT_CURRENT: "default_current_raw",
        REG_AUTO_PHASE_SWITCH: "auto_phase_switch",
        REG_MIN_SWITCH_INTERVAL: "min_switch_interval",
    }

    _VERIFY_AND_RESTORE_REGISTERS = (
        REG_WORK_MODE,
        REG_TIME_VALIDITY,
        REG_DEFAULT_CURRENT,
        REG_AUTO_PHASE_SWITCH,
        REG_MIN_SWITCH_INTERVAL,
    )
    
    _DYNAMIC_BLOCK = (
        REG_MAX_CHARGING_CURRENT,
        REG_MAX_CHARGING_POWER,
    )
    
    _SESSION_BLOCK = (
        REG_ALLOWED_CHARGE_TIME,
        REG_ALLOWED_CHARGE_ENERGY,
    )
    
    _ACTIVE_CHARGING_STATES = {2, 3, 4}

    def __init__(self, hass: HomeAssistant, client: FoxESSModbusClient,
                 scan_interval: int, entry_id: str) -> None:
        self.client = client
        self.command_cache: dict[int, int] = {}
        self._store = Store(
            hass,
            1,
            f"{DOMAIN}.{entry_id}.command_cache",
        )
    
        self._last_status: int | None = None
        # True means use the stored time and energy limits.
        # False means send 0xFFFF to both registers to disable them.
        self.session_limits_enabled = False
        # Set when the enable switch or either session-limit number changes.
        self._session_limits_pending = False     
                     
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    async def async_load_command_cache(self) -> None:
        """Restore desired writable settings from Home Assistant storage."""
        stored = await self._store.async_load()
    
        if not isinstance(stored, dict):
            return
    
        commands = stored.get("commands", {})
        
        if isinstance(commands, dict):
            for address, value in commands.items():
                try:
                    register = int(address)
                    raw_value = int(value)
                except (TypeError, ValueError):
                    continue
        
                if register in self._REGISTER_TO_DATA_KEY:
                    self.command_cache[register] = raw_value
        
        enabled = stored.get("session_limits_enabled")
        
        if isinstance(enabled, bool):
            self.session_limits_enabled = enabled
    
        _LOGGER.debug(
            "Restored FoxESS desired settings: commands=%s, "
            "session_limits_enabled=%s",
            self.command_cache,
            self.session_limits_enabled,
        )

    async def _async_save_command_cache(self) -> None:
        """Persist desired register values and session-limit state."""
        await self._store.async_save({
            "commands": {
                str(register): raw_value
                for register, raw_value in self.command_cache.items()
            },
            "session_limits_enabled": self.session_limits_enabled,
        })
    
    async def async_cache_register(
        self,
        address: int,
        value: int,
        data_key: str,
    ) -> None:
        """Save a desired setting without immediately writing Modbus."""
        if address not in self._REGISTER_TO_DATA_KEY:
            raise ValueError(
                f"Register 0x{address:04X} is not cacheable"
            )
    
        self.command_cache[address] = value
    
        if address in self._SESSION_BLOCK:
            self._session_limits_pending = True
    
        await self._async_save_command_cache()
    
        _LOGGER.debug(
            "Cached desired FoxESS setting 0x%04X=%d; "
            "no immediate Modbus write",
            address,
            value,
        )

    async def async_set_session_limits_enabled(
        self,
        enabled: bool,
    ) -> None:
        """Store whether session time and energy limits are enabled."""
        self.session_limits_enabled = enabled
        self._session_limits_pending = True
    
        await self._async_save_command_cache()
    
        _LOGGER.debug(
            "Cached session limits enabled=%s; "
            "change will be sent on the next applicable poll",
            enabled,
        )
    
    def desired_or_actual(
        self,
        address: int,
        data_key: str,
    ) -> int | None:
        """Return HA desired value when set, otherwise charger readback."""
        if address in self.command_cache:
            return self.command_cache[address]
    
        return (self.data or {}).get(data_key)

    @staticmethod
    def _block_values(
        registers: tuple[int, ...],
        desired: dict[int, int],
        actual: dict[int, int],
    ) -> list[int] | None:
        """Build a complete block from desired then actual values."""
        values: list[int] = []
    
        for register in registers:
            if register in desired:
                values.append(desired[register])
            elif register in actual:
                values.append(actual[register])
            else:
                return None
    
        return values
    
    async def _async_update_data(self) -> dict:
        try:
            desired = dict(self.command_cache)
            previous_status = self._last_status
            session_limits_pending = self._session_limits_pending
            session_limits_enabled = self.session_limits_enabled
    
            data, current_status, session_limits_written = (
                await self.hass.async_add_executor_job(
                    self._fetch,
                    desired,
                    previous_status,
                    session_limits_pending,
                    session_limits_enabled,
                )
            )
    
            self._last_status = current_status
    
            if session_limits_written:
                self._session_limits_pending = False
    
            return data
    
        except Exception as err:
            raise UpdateFailed(f"Modbus error: {err}") from err

    def _fetch(
        self,
        desired: dict[int, int],
        previous_status: int | None,
        session_limits_pending: bool,
        session_limits_enabled: bool,
    ) -> tuple[dict, int | None, bool]:
        # Start from the last known-good values instead of a blank dict, so a
        # single failed register-block read doesn't wipe out everything else
        # that's still valid (e.g. right after a write, before the charger's
        # ready to answer the follow-up read).
        data: dict = dict(self.data) if self.data else {}
        current_status: int | None = previous_status
        session_limits_written = False

        # ── 0x1000–0x1015: 22 Status-Register ────────────────────────────────
        regs = self.client.read_registers(0x1000, 22)
        if regs and len(regs) >= 22:
            data["device_address"]  = regs[0]
            data["software_version"]= regs[1]
            data["stop_reason"]     = regs[2]
            data["status"]          = regs[3]
            current_status          = regs[3]
            data["cp_status"]       = regs[4]
            data["cc_status"]       = regs[5]
            data["port_temp_raw"]   = regs[6]
            data["ambient_temp_raw"]= regs[7]
            data["l1_voltage_raw"]  = regs[8]
            data["l2_voltage_raw"]  = regs[9]
            data["l3_voltage_raw"]  = regs[10]
            data["l1_current_raw"]  = regs[11]
            data["l2_current_raw"]  = regs[12]
            data["l3_current_raw"]  = regs[13]
            data["power_raw"]       = regs[14]
            data["lock_status"]     = regs[15]
            data["phase_sequence"]  = regs[16]
            data["max_power_raw"]   = regs[17]
            data["min_power_raw"]   = regs[18]
            data["max_current_raw"] = regs[19]
            data["min_current_raw"] = regs[20]
            data["alarm_code"]      = regs[21]
        else:
            _LOGGER.warning("Could not read status registers 0x1000–0x1015")

        # ── 0x1016/0x1018/0x101A/0x101C: UINT32 Register ─────────────────────
        for key, addr in [
            ("current_energy_raw", 0x1016),
            ("total_energy_raw",   0x1018),
            ("fault_code",         0x101A),
            ("rfid_card",          0x101C),
        ]:
            val = self.client.read_uint32(addr)
            if val is not None:
                data[key] = val

        # ── 0x3000–0x300B: R/W Config Register ───────────────────────────────
        cfg = self.client.read_registers(0x3000, 12)
        actual: dict[int, int] = {}
        if cfg and len(cfg) >= 12:
            for offset, value in enumerate(cfg):
                actual[0x3000 + offset] = value
            data["work_mode"]                = cfg[0]
            data["max_charging_current_raw"] = cfg[1]
            data["max_charging_power_raw"]   = cfg[2]
            data["allowed_charge_time"]      = cfg[3]
            data["allowed_charge_energy"]    = cfg[4]
            data["time_validity"]            = cfg[5]
            data["default_current_raw"]      = cfg[6]
            # cfg[7..9] reserviert
            data["auto_phase_switch"]        = cfg[10]
            data["min_switch_interval"]      = cfg[11]
        else:
            _LOGGER.warning("Could not read config registers 0x3000–0x300B")
        
        for register in self._VERIFY_AND_RESTORE_REGISTERS:
            if register not in desired:
                continue
        
            if actual.get(register) == desired[register]:
                continue
        
            success = self.client.write_holding_registers(
                register,
                [desired[register]],
            )

            if not success:
                _LOGGER.warning(
                    "Could not restore setting 0x%04X=%d",
                    register,
                    desired[register],
                )

        dynamic_values = self._block_values(
            self._DYNAMIC_BLOCK,
            desired,
            actual,
        )
        
        if dynamic_values is not None:
            success = self.client.write_holding_registers(
                REG_MAX_CHARGING_CURRENT,
                dynamic_values,
            )
        
            if not success:
                _LOGGER.warning(
                    "Could not refresh dynamic block 0x3001-0x3002"
                )

        charging_started = (
            current_status is not None
            and current_status in self._ACTIVE_CHARGING_STATES
            and previous_status not in self._ACTIVE_CHARGING_STATES
        )
        
        should_write_session_limits = (
            current_status in self._ACTIVE_CHARGING_STATES
            and (
                charging_started
                or session_limits_pending
            )
        )
        
        if should_write_session_limits:
            if session_limits_enabled:
                session_values = self._block_values(
                    self._SESSION_BLOCK,
                    desired,
                    actual,
                )
            else:
                # The protocol defines 0xFFFF as disabled for both limits.
                session_values = [0xFFFF, 0xFFFF]
        
            if session_values is not None:
                session_limits_written = (
                    self.client.write_holding_registers(
                        REG_ALLOWED_CHARGE_TIME,
                        session_values,
                    )
                )
        
                if session_limits_written:
                    _LOGGER.debug(
                        "Wrote session block 0x3003-0x3004: "
                        "enabled=%s values=%s",
                        session_limits_enabled,
                        session_values,
                    )
                else:
                    _LOGGER.warning(
                        "Could not write session block 0x3003-0x3004"
                    )
        
        return data, current_status, session_limits_written
