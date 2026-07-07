from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MonitorSpec:
    id: str
    name: str
    priority: str
    severity: str
    implemented: bool
    description: str


MONITOR_SPECS = [
    MonitorSpec("feed_reachable", "Feed reachable", "platform", "critical", True, "Input feed can be ingested."),
    MonitorSpec("essence_video_present", "Video present", "platform", "critical", True, "Expected video essence is present."),
    MonitorSpec("essence_audio_present", "Audio present", "platform", "critical", True, "Expected audio essence is present."),
    MonitorSpec("essence_captions_present", "Captions present", "platform", "major", True, "Expected captions are present."),
    MonitorSpec("black_video_detected", "Black video detected", "platform", "major", True, "Expected black-video profile validates."),
    MonitorSpec("frozen_video_detected", "Frozen video detected", "platform", "major", True, "Expected frozen-video profile validates."),
    MonitorSpec("video_frame_rate_match", "Video frame rate match", "platform", "major", True, "Measured video frame rate matches the configured feed frame rate."),
    MonitorSpec("loudness_bs1770_measurement", "ITU-R BS.1770 loudness measurement", "audio loudness", "major", True, "Audio loudness can be measured with a BS.1770-compatible meter."),
    MonitorSpec("loudness_ebu_r128_integrated", "EBU R 128 integrated loudness", "audio loudness", "major", True, "Integrated loudness is within the EBU R 128 target window."),
    MonitorSpec("loudness_ebu_r128_true_peak", "EBU R 128 true peak", "audio loudness", "major", True, "True peak does not exceed the EBU R 128 maximum."),
    MonitorSpec("loudness_atsc_a85_integrated", "ATSC A/85 integrated loudness", "audio loudness", "major", True, "Integrated loudness is within the ATSC A/85 target window."),
    MonitorSpec("tr101_1_1_ts_sync_loss", "TS sync loss", "TR101 priority 1", "critical", True, "Loss of MPEG-2 TS synchronization."),
    MonitorSpec("tr101_1_2_sync_byte_error", "Sync byte error", "TR101 priority 1", "critical", True, "Sync byte not equal to 0x47."),
    MonitorSpec("tr101_1_3_pat_error", "PAT error", "TR101 priority 1", "critical", True, "PAT missing, wrong table id, or scrambled."),
    MonitorSpec("tr101_1_3a_pat_error_2", "PAT error 2", "TR101 priority 1", "critical", True, "Updated PAT repetition/table-id check."),
    MonitorSpec("tr101_1_4_continuity_count_error", "Continuity count error", "TR101 priority 1", "critical", True, "Incorrect order, repeat, or lost packet."),
    MonitorSpec("tr101_1_5_pmt_error", "PMT error", "TR101 priority 1", "critical", True, "PMT missing or scrambled."),
    MonitorSpec("tr101_1_5a_pmt_error_2", "PMT error 2", "TR101 priority 1", "critical", True, "Updated PMT repetition/table-id check."),
    MonitorSpec("tr101_1_6_pid_error", "PID error", "TR101 priority 1", "critical", True, "Referenced PID missing for configured period."),
    MonitorSpec("tr101_2_1_transport_error", "Transport error", "TR101 priority 2", "major", True, "Transport error indicator set."),
    MonitorSpec("tr101_2_2_crc_error", "CRC error", "TR101 priority 2", "major", True, "PSI/SI table CRC error."),
    MonitorSpec("tr101_2_3_pcr_error", "PCR error", "TR101 priority 2", "major", True, "PCR discontinuity or repetition fault."),
    MonitorSpec("tr101_2_3a_pcr_repetition_error", "PCR repetition error", "TR101 priority 2", "major", True, "PCR interval greater than 100 ms."),
    MonitorSpec("tr101_2_3b_pcr_discontinuity_indicator_error", "PCR discontinuity indicator error", "TR101 priority 2", "major", True, "PCR jump without discontinuity indicator."),
    MonitorSpec("tr101_2_4_pcr_accuracy_error", "PCR accuracy error", "TR101 priority 2", "major", True, "PCR accuracy outside +/-500 ns."),
    MonitorSpec("tr101_2_5_pts_error", "PTS error", "TR101 priority 2", "major", True, "PTS repetition period greater than 700 ms."),
    MonitorSpec("tr101_2_6_cat_error", "CAT error", "TR101 priority 2", "major", True, "CAT missing or malformed for scrambled packets."),
    MonitorSpec("tr101_3_1_nit_error", "NIT error", "TR101 priority 3", "minor", True, "NIT table-id, presence, or repetition fault."),
    MonitorSpec("tr101_3_1a_nit_actual_error", "NIT actual error", "TR101 priority 3", "minor", True, "NIT_actual table-id, presence, or repetition fault."),
    MonitorSpec("tr101_3_1b_nit_other_error", "NIT other error", "TR101 priority 3", "minor", True, "NIT_other repetition fault when present."),
    MonitorSpec("tr101_3_2_si_repetition_error", "SI repetition error", "TR101 priority 3", "minor", True, "SI table repetition outside parser-backed limits."),
    MonitorSpec("tr101_3_3_buffer_error", "Buffer error", "TR101 priority 3", "minor", True, "Parser-backed T-STD timing buffer pressure check."),
    MonitorSpec("tr101_3_4_unreferenced_pid", "Unreferenced PID", "TR101 priority 3", "minor", True, "PID is not referenced by PMT/CAT within the sample window."),
    MonitorSpec("tr101_3_4a_unreferenced_pid", "Unreferenced PID 2", "TR101 priority 3", "minor", True, "Updated unreferenced PID check."),
    MonitorSpec("tr101_3_5_sdt_error", "SDT error", "TR101 priority 3", "minor", True, "SDT table-id, presence, or repetition fault."),
    MonitorSpec("tr101_3_5a_sdt_actual_error", "SDT actual error", "TR101 priority 3", "minor", True, "SDT_actual table-id, presence, or repetition fault."),
    MonitorSpec("tr101_3_5b_sdt_other_error", "SDT other error", "TR101 priority 3", "minor", True, "SDT_other repetition fault when present."),
    MonitorSpec("tr101_3_6_eit_error", "EIT error", "TR101 priority 3", "minor", True, "EIT table-id, presence, or repetition fault."),
    MonitorSpec("tr101_3_6a_eit_actual_error", "EIT actual error", "TR101 priority 3", "minor", True, "EIT actual P/F table-id, presence, or repetition fault."),
    MonitorSpec("tr101_3_6b_eit_other_error", "EIT other error", "TR101 priority 3", "minor", True, "EIT other P/F repetition fault when present."),
    MonitorSpec("tr101_3_6c_eit_pf_error", "EIT P/F error", "TR101 priority 3", "minor", True, "EIT present/following section pair is incomplete."),
    MonitorSpec("tr101_3_7_rst_error", "RST error", "TR101 priority 3", "minor", True, "RST table-id or repetition fault."),
    MonitorSpec("tr101_3_8_tdt_error", "TDT error", "TR101 priority 3", "minor", True, "TDT/TOT table-id, presence, or repetition fault."),
    MonitorSpec("tr101_3_9_empty_buffer_error", "Empty buffer error", "TR101 priority 3", "minor", True, "Parser-backed T-STD empty-buffer timing check."),
    MonitorSpec("tr101_3_10_data_delay_error", "Data delay error", "TR101 priority 3", "minor", True, "Parser-backed T-STD data delay timing check."),
]

SPEC_BY_ID = {spec.id: spec for spec in MONITOR_SPECS}


def monitor_catalog_payload() -> list[dict]:
    return [asdict(spec) for spec in MONITOR_SPECS]
