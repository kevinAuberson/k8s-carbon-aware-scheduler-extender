"""
File:        tests/test_vsphere.py
Author:      Kevin Auberson
Created:     2026-05-21
Version:     0.1.0
Description: Unit tests for the vSphere client. The vCenter connection and
             the pyVmomi objects are mocked so the tests run without a real
             vCenter. The focus is on the per-VM watt estimation logic.
"""

import os
from unittest.mock import MagicMock

os.environ.setdefault("VCENTER_HOST", "fake-vcenter")
os.environ.setdefault("VCENTER_USER", "fake-user")
os.environ.setdefault("VCENTER_PASSWORD", "fake-pass")

from cache import cache
from vsphere import VSphere


def _fake_host(name, watts, cpu_mhz, num_cores):
    """Build a fake ESXi host object as pyVmomi would expose it."""
    host = MagicMock()
    host.name = name
    host.summary.hardware.cpuMhz = cpu_mhz
    host.summary.hardware.numCpuCores = num_cores
    return host


def _fake_vm(name, host_name, power_state="poweredOn"):
    """Build a fake VM object as pyVmomi would expose it."""
    vm = MagicMock()
    vm.name = name
    vm.runtime.powerState = power_state
    vm.runtime.host.name = host_name
    return vm


def test_get_host_power_computes_total_mhz():
    """Host total MHz = clock per core * number of cores."""
    vs = VSphere()
    cache._store.clear()

    vs._connect = MagicMock()
    vs._content = MagicMock()
    fake_host = _fake_host("esxi-1", watts=250.0, cpu_mhz=2400, num_cores=20)
    vs._get_all = MagicMock(return_value=[fake_host])

    vs._query_stats = MagicMock(return_value={"power.power.average": 250.0})

    result = vs.get_host_power()

    assert result["esxi-1"]["watts"] == 250.0
    assert result["esxi-1"]["total_mhz"] == 2400 * 20  # 48000


def test_vm_watts_estimation_ratio():
    """
    VM watts = (vm_cpu_mhz / host_total_mhz) * host_watts.

    Example: VM uses 4800 MHz on a host of 48000 MHz total drawing 200 W.
    Ratio = 4800 / 48000 = 0.1, so VM watts = 0.1 * 200 = 20 W.
    """
    vs = VSphere()
    cache._store.clear()

    vs._connect = MagicMock()
    vs._content = MagicMock()

    fake_vm = _fake_vm("vm-1", host_name="esxi-1")
    vs._get_all = MagicMock(return_value=[fake_vm])

    vs.get_host_power = MagicMock(
        return_value={"esxi-1": {"watts": 200.0, "total_mhz": 48000}}
    )

    vs._query_stats = MagicMock(
        return_value={
            "cpu.usagemhz.average": 4800,
            "mem.consumed.average": 2 * 1024 * 1024,
        }
    )

    result = vs.get_vm_estimated_watts()

    assert len(result) == 1
    assert result[0]["name"] == "vm-1"
    assert result[0]["host"] == "esxi-1"
    assert result[0]["watts"] == 20.0  # 0.1 * 200


def test_powered_off_vms_are_skipped():
    """VMs that are not powered on are excluded from the result."""
    vs = VSphere()
    cache._store.clear()

    vs._connect = MagicMock()
    vs._content = MagicMock()

    running_vm = _fake_vm("vm-on", host_name="esxi-1", power_state="poweredOn")
    stopped_vm = _fake_vm("vm-off", host_name="esxi-1", power_state="poweredOff")
    vs._get_all = MagicMock(return_value=[running_vm, stopped_vm])

    vs.get_host_power = MagicMock(
        return_value={"esxi-1": {"watts": 200.0, "total_mhz": 48000}}
    )
    vs._query_stats = MagicMock(
        return_value={
            "cpu.usagemhz.average": 4800,
            "mem.consumed.average": 1024 * 1024,
        }
    )

    result = vs.get_vm_estimated_watts()

    names = [vm["name"] for vm in result]
    assert "vm-on" in names
    assert "vm-off" not in names


def test_vm_watts_zero_when_host_unknown():
    """If the VM's host is not in the host list, watts default to 0."""
    vs = VSphere()
    cache._store.clear()

    vs._connect = MagicMock()
    vs._content = MagicMock()

    fake_vm = _fake_vm("vm-1", host_name="unknown-host")
    vs._get_all = MagicMock(return_value=[fake_vm])

    vs.get_host_power = MagicMock(
        return_value={"esxi-1": {"watts": 200.0, "total_mhz": 48000}}
    )
    vs._query_stats = MagicMock(
        return_value={
            "cpu.usagemhz.average": 4800,
            "mem.consumed.average": 1024 * 1024,
        }
    )

    result = vs.get_vm_estimated_watts()

    assert result[0]["watts"] == 0
