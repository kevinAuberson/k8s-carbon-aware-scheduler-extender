"""
File:        tests/test_main.py
Author:      Kevin Auberson
Created:     2026-05-21
Version:     0.1.0
Description: Unit tests for the aggregator entry point. Covers the node
             mapping loader and the build_signal aggregation logic, with
             all data sources mocked.
"""

import os
from unittest.mock import MagicMock

os.environ.setdefault("EMAPS_TOKEN", "test-token")
os.environ.setdefault("VCENTER_HOST", "fake-vcenter")
os.environ.setdefault("VCENTER_USER", "fake-user")
os.environ.setdefault("VCENTER_PASSWORD", "fake-pass")

import main


def test_load_node_mapping_valid_file(tmp_path):
    """A well-formed YAML file is parsed into a dict."""
    yaml_file = tmp_path / "mapping.yaml"
    yaml_file.write_text(
        "mapping:\n  k8s-node-1: vsphere-vm-1\n  k8s-node-2: vsphere-vm-2\n"
    )

    result = main.load_node_mapping(str(yaml_file))

    assert result == {
        "k8s-node-1": "vsphere-vm-1",
        "k8s-node-2": "vsphere-vm-2",
    }


def test_load_node_mapping_missing_file_returns_empty():
    """A missing file returns an empty dict instead of crashing."""
    result = main.load_node_mapping("/does/not/exist.yaml")
    assert result == {}


def test_load_node_mapping_empty_mapping_key(tmp_path):
    """A file without a 'mapping' key returns an empty dict."""
    yaml_file = tmp_path / "empty.yaml"
    yaml_file.write_text("something_else: true\n")

    result = main.load_node_mapping(str(yaml_file))
    assert result == {}


def _make_mocks(grid_intensity=92, vm_watts=50.0, cpu=500, mem=1024):
    """Create mocked clients returning controlled data for one node."""
    emaps = MagicMock()
    emaps.get_current.return_value = {
        "carbon_intensity": grid_intensity,
        "zone": "CH",
    }
    emaps.get_forecast_24h.return_value = []

    vsphere = MagicMock()
    vsphere.get_vm_estimated_watts.return_value = [
        {
            "name": "vsphere-vm-1",
            "host": "esxi-1",
            "watts": vm_watts,
            "cpu_mhz": 4800,
            "memory_mib": 2048,
        },
    ]

    metrics = MagicMock()
    metrics.get_node_usage.return_value = {
        "k8s-node-1": {"cpu_millicores": cpu, "memory_mib": mem},
    }

    return emaps, vsphere, metrics


def test_build_signal_combines_sources():
    """build_signal merges grid intensity, vSphere watts and metrics CPU/RAM."""
    emaps, vsphere, metrics = _make_mocks(grid_intensity=92, vm_watts=50.0)
    mapping = {"k8s-node-1": "vsphere-vm-1"}

    signal = main.build_signal(emaps, vsphere, metrics, mapping)

    assert signal["zone"] == "CH"
    assert signal["grid_intensity_g_per_kwh"] == 92
    assert len(signal["nodes"]) == 1

    node = signal["nodes"][0]
    assert node["name"] == "k8s-node-1"
    assert node["vsphere_name"] == "vsphere-vm-1"
    assert node["watts"] == 50.0
    assert node["cpu_millicores"] == 500
    assert node["memory_mib"] == 1024


def test_build_signal_co2_calculation():
    """
    CO2 rate = watts * grid_intensity / (3600 * 1000).

    Example: 50 W at 92 gCO2/kWh
           = 50 * 92 / 3'600'000
           = 4600 / 3'600'000
           = 0.001277... gCO2/s
    """
    emaps, vsphere, metrics = _make_mocks(grid_intensity=92, vm_watts=50.0)
    mapping = {"k8s-node-1": "vsphere-vm-1"}

    signal = main.build_signal(emaps, vsphere, metrics, mapping)

    expected = 50.0 * 92 / (3600 * 1000)
    assert abs(signal["nodes"][0]["co2_g_per_s"] - expected) < 1e-9


def test_build_signal_unmapped_node_gets_zero_watts():
    """A K8s node with no vSphere mapping reports 0 watts."""
    emaps, vsphere, metrics = _make_mocks()
    mapping = {}

    signal = main.build_signal(emaps, vsphere, metrics, mapping)

    assert signal["nodes"][0]["watts"] == 0.0
    assert signal["nodes"][0]["co2_g_per_s"] == 0.0


def test_build_signal_emaps_failure_uses_fallback():
    """If Electricity Maps fails, build_signal uses the fallback intensity."""
    emaps, vsphere, metrics = _make_mocks()
    emaps.get_current.side_effect = Exception("API down")
    mapping = {"k8s-node-1": "vsphere-vm-1"}

    signal = main.build_signal(emaps, vsphere, metrics, mapping)

    assert signal["grid_intensity_g_per_kwh"] == 100.0
    assert signal["zone"] == "CH"


def test_build_signal_vsphere_failure_keeps_going():
    """If vSphere fails, nodes still appear with 0 watts (CPU/RAM intact)."""
    emaps, vsphere, metrics = _make_mocks()
    vsphere.get_vm_estimated_watts.side_effect = Exception("vCenter timeout")
    mapping = {"k8s-node-1": "vsphere-vm-1"}

    signal = main.build_signal(emaps, vsphere, metrics, mapping)

    node = signal["nodes"][0]
    assert node["watts"] == 0.0  # vSphere failed
    assert node["cpu_millicores"] == 500  # but metrics-server still worked
