from __future__ import annotations

from dataclasses import dataclass, field


PACKET_SIZE = 188
PCR_LIMIT_SECONDS = 0.1
PCR_ACCURACY_LIMIT_SECONDS = 0.0000005
PTS_LIMIT_SECONDS = 0.7
SI_REPEAT_MIN_SECONDS = 0.025
UNREFERENCED_PID_SECONDS = 0.5
NIT_REPEAT_SECONDS = 10
SDT_EIT_REPEAT_SECONDS = 2
OTHER_SI_REPEAT_SECONDS = 10
TDT_REPEAT_SECONDS = 30


@dataclass
class TR101Report:
    indicators: dict[str, bool] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)

    def set(self, indicator: str, message: str):
        self.indicators[indicator] = True
        self.messages.setdefault(indicator, message)


def analyze_ts(data: bytes, sample_seconds: float | None = None) -> TR101Report:
    report = TR101Report()
    packets = [data[index : index + PACKET_SIZE] for index in range(0, len(data), PACKET_SIZE) if len(data[index : index + PACKET_SIZE]) == PACKET_SIZE]
    if not packets:
        report.set("tr101_1_1_ts_sync_loss", "No complete MPEG-TS packets in sample")
        for indicator in TR101_INDICATORS:
            report.indicators.setdefault(indicator, False)
        return report

    pat_seen = False
    pmt_seen = False
    cat_seen = False
    scrambled_seen = False
    pmt_pids = set()
    cat_pids = set()
    referenced_pids = set()
    seen_pids = set()
    pcr_pids = set()
    continuity = {}
    pcr_seen = {}
    pts_seen = {}
    si_tables_seen = {}
    si_section_counts = {}
    si_section_last_packet = {}
    byte_rate = (len(packets) * PACKET_SIZE / sample_seconds) if sample_seconds and sample_seconds > 0 else None
    bad_sync_run = 0
    good_sync_run = 0

    for packet_index, packet in enumerate(packets):
        if packet[0] != 0x47:
            bad_sync_run += 1
            good_sync_run = 0
            report.set("tr101_1_2_sync_byte_error", "Sync byte is not 0x47")
            if bad_sync_run >= 2:
                report.set("tr101_1_1_ts_sync_loss", "Two or more consecutive sync-byte errors")
            continue
        good_sync_run += 1
        bad_sync_run = 0
        if good_sync_run < 5:
            report.indicators.setdefault("tr101_1_1_ts_sync_loss", False)

        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        seen_pids.add(pid)
        transport_error = bool(packet[1] & 0x80)
        payload_start = bool(packet[1] & 0x40)
        scrambling_control = (packet[3] >> 6) & 0x03
        adaptation_control = (packet[3] >> 4) & 0x03
        continuity_counter = packet[3] & 0x0F
        has_adaptation = adaptation_control in {2, 3}
        has_payload = adaptation_control in {1, 3}
        discontinuity = False
        payload_offset = 4

        if transport_error:
            report.set("tr101_2_1_transport_error", "Transport error indicator is set")
        if scrambling_control:
            scrambled_seen = True
            if pid == 0:
                report.set("tr101_1_3_pat_error", "PAT PID is scrambled")
                report.set("tr101_1_3a_pat_error_2", "PAT PID is scrambled")
            if pid in pmt_pids:
                report.set("tr101_1_5_pmt_error", "PMT PID is scrambled")
                report.set("tr101_1_5a_pmt_error_2", "PMT PID is scrambled")

        if has_adaptation:
            adaptation_length = packet[4]
            if adaptation_length and 5 + adaptation_length <= PACKET_SIZE:
                flags = packet[5]
                discontinuity = bool(flags & 0x80)
                pcr_flag = bool(flags & 0x10)
                if pcr_flag and adaptation_length >= 7:
                    pcr = parse_pcr(packet[6:12])
                    previous = pcr_seen.get(pid)
                    if previous:
                        previous_pcr, previous_packet_index = previous
                        pcr_delta = pcr - previous_pcr
                        if pcr_delta > PCR_LIMIT_SECONDS:
                            report.set("tr101_2_3a_pcr_repetition_error", "PCR repetition interval is greater than 100 ms")
                            report.set("tr101_2_3_pcr_error", "PCR repetition interval is greater than 100 ms")
                        if pcr_delta > PCR_LIMIT_SECONDS and not discontinuity:
                            report.set("tr101_2_3b_pcr_discontinuity_indicator_error", "PCR jump greater than 100 ms without discontinuity indicator")
                            report.set("tr101_2_3_pcr_error", "PCR jump greater than 100 ms without discontinuity indicator")
                        if byte_rate:
                            arrival_delta = ((packet_index - previous_packet_index) * PACKET_SIZE) / byte_rate
                            if abs(pcr_delta - arrival_delta) > PCR_ACCURACY_LIMIT_SECONDS:
                                report.set("tr101_2_4_pcr_accuracy_error", "PCR timing differs from packet-rate clock by more than 500 ns")
                    pcr_seen[pid] = (pcr, packet_index)
            payload_offset += 1 + adaptation_length

        if has_payload:
            previous = continuity.get(pid)
            if previous:
                previous_counter, duplicate_count = previous
                expected = (previous_counter + 1) % 16
                if continuity_counter == previous_counter:
                    duplicate_count += 1
                    if duplicate_count > 2:
                        report.set("tr101_1_4_continuity_count_error", f"PID {pid} repeated continuity counter more than twice")
                elif continuity_counter != expected and not discontinuity:
                    report.set("tr101_1_4_continuity_count_error", f"PID {pid} continuity counter jumped")
                    duplicate_count = 1
                else:
                    duplicate_count = 1
                continuity[pid] = (continuity_counter, duplicate_count)
            else:
                continuity[pid] = (continuity_counter, 1)

        if not has_payload or payload_offset >= PACKET_SIZE:
            continue

        payload = packet[payload_offset:]
        if pid == 0 and payload_start:
            section = section_from_payload(payload)
            if not section:
                report.set("tr101_1_3_pat_error", "PAT section missing")
                report.set("tr101_1_3a_pat_error_2", "PAT section missing")
            elif section[0] != 0x00:
                report.set("tr101_1_3_pat_error", "PID 0x0000 does not contain table_id 0x00")
                report.set("tr101_1_3a_pat_error_2", "PID 0x0000 does not contain table_id 0x00")
            else:
                pat_seen = True
                if mpeg_crc32(section) != 0:
                    report.set("tr101_2_2_crc_error", "PAT CRC check failed")
                pmt_pids.update(parse_pat(section))

        if pid in pmt_pids and payload_start:
            section = section_from_payload(payload)
            if not section:
                report.set("tr101_1_5_pmt_error", "PMT section missing")
                report.set("tr101_1_5a_pmt_error_2", "PMT section missing")
            elif section[0] != 0x02:
                report.set("tr101_1_5_pmt_error", "PMT PID does not contain table_id 0x02")
                report.set("tr101_1_5a_pmt_error_2", "PMT PID does not contain table_id 0x02")
            else:
                pmt_seen = True
                if mpeg_crc32(section) != 0:
                    report.set("tr101_2_2_crc_error", "PMT CRC check failed")
                pcr_pid, pids = parse_pmt(section)
                if pcr_pid is not None:
                    pcr_pids.add(pcr_pid)
                referenced_pids.update(pids)

        if pid == 1 and payload_start:
            section = section_from_payload(payload)
            if section and section[0] == 0x01:
                cat_seen = True
                if mpeg_crc32(section) != 0:
                    report.set("tr101_2_2_crc_error", "CAT CRC check failed")
                else:
                    cat_pids.update(parse_cat(section))
            elif section:
                report.set("tr101_2_6_cat_error", "PID 0x0001 contains a non-CAT section")

        if pid in SI_PIDS and payload_start:
            section = section_from_payload(payload)
            if section:
                check_si_section(report, pid, section)
                table_id = section[0]
                section_key = (pid, table_id, section_number(section))
                si_tables_seen.setdefault(pid, set()).add(table_id)
                si_section_counts[section_key] = si_section_counts.get(section_key, 0) + 1
                previous_packet_index = si_section_last_packet.get(section_key)
                if previous_packet_index is not None and byte_rate:
                    arrival_delta = ((packet_index - previous_packet_index) * PACKET_SIZE) / byte_rate
                    check_si_repeat(report, pid, table_id, arrival_delta)
                si_section_last_packet[section_key] = packet_index
                if section_has_crc(section) and mpeg_crc32(section) != 0:
                    report.set("tr101_2_2_crc_error", "SI table CRC check failed")

        if pid in referenced_pids and payload_start:
            pts = parse_pts(payload)
            if pts is not None:
                previous_pts = pts_seen.get(pid)
                if previous_pts is not None and pts - previous_pts > PTS_LIMIT_SECONDS:
                    report.set("tr101_2_5_pts_error", "PTS repetition period is greater than 700 ms")
                pts_seen[pid] = pts

    if not pat_seen:
        report.set("tr101_1_3_pat_error", "PAT did not occur in sample")
        report.set("tr101_1_3a_pat_error_2", "PAT did not occur in sample")
    if pmt_pids and not pmt_seen:
        report.set("tr101_1_5_pmt_error", "PMT did not occur in sample")
        report.set("tr101_1_5a_pmt_error_2", "PMT did not occur in sample")
    missing = sorted(referenced_pids - seen_pids)
    if missing:
        report.set("tr101_1_6_pid_error", f"Referenced PID(s) missing: {', '.join(str(pid) for pid in missing)}")
    missing_pcr = sorted(pcr_pids - pcr_seen.keys())
    if missing_pcr:
        report.set("tr101_2_3a_pcr_repetition_error", f"PCR PID(s) missing: {', '.join(str(pid) for pid in missing_pcr)}")
        report.set("tr101_2_3_pcr_error", f"PCR PID(s) missing: {', '.join(str(pid) for pid in missing_pcr)}")
    if scrambled_seen and not cat_seen:
        report.set("tr101_2_6_cat_error", "Scrambled packets are present but CAT is missing")
    if sample_seconds is None or sample_seconds >= UNREFERENCED_PID_SECONDS:
        unreferenced = sorted(seen_pids - referenced_pids - pcr_pids - pmt_pids - cat_pids - set(range(0x20)) - {0x1FFF})
        if pmt_seen and unreferenced:
            message = f"PID(s) not referenced by PMT/CAT: {', '.join(str(pid) for pid in unreferenced)}"
            report.set("tr101_3_4_unreferenced_pid", message)
            report.set("tr101_3_4a_unreferenced_pid", message)
    check_si_presence(report, seen_pids, si_tables_seen, si_section_counts, sample_seconds)

    for indicator in TR101_INDICATORS:
        report.indicators.setdefault(indicator, False)
    return report


TR101_INDICATORS = (
    "tr101_1_1_ts_sync_loss",
    "tr101_1_2_sync_byte_error",
    "tr101_1_3_pat_error",
    "tr101_1_3a_pat_error_2",
    "tr101_1_4_continuity_count_error",
    "tr101_1_5_pmt_error",
    "tr101_1_5a_pmt_error_2",
    "tr101_1_6_pid_error",
    "tr101_2_1_transport_error",
    "tr101_2_2_crc_error",
    "tr101_2_3_pcr_error",
    "tr101_2_3a_pcr_repetition_error",
    "tr101_2_3b_pcr_discontinuity_indicator_error",
    "tr101_2_4_pcr_accuracy_error",
    "tr101_2_5_pts_error",
    "tr101_2_6_cat_error",
    "tr101_3_1_nit_error",
    "tr101_3_1a_nit_actual_error",
    "tr101_3_1b_nit_other_error",
    "tr101_3_2_si_repetition_error",
    "tr101_3_3_buffer_error",
    "tr101_3_4_unreferenced_pid",
    "tr101_3_4a_unreferenced_pid",
    "tr101_3_5_sdt_error",
    "tr101_3_5a_sdt_actual_error",
    "tr101_3_5b_sdt_other_error",
    "tr101_3_6_eit_error",
    "tr101_3_6a_eit_actual_error",
    "tr101_3_6b_eit_other_error",
    "tr101_3_6c_eit_pf_error",
    "tr101_3_7_rst_error",
    "tr101_3_8_tdt_error",
    "tr101_3_9_empty_buffer_error",
    "tr101_3_10_data_delay_error",
)

SI_PIDS = {0x0010, 0x0011, 0x0012, 0x0013, 0x0014}


def section_from_payload(payload: bytes) -> bytes:
    if not payload:
        return b""
    pointer = payload[0]
    start = 1 + pointer
    if start + 3 > len(payload):
        return b""
    section_length = ((payload[start + 1] & 0x0F) << 8) | payload[start + 2]
    end = start + 3 + section_length
    return payload[start:end] if end <= len(payload) else b""


def parse_pat(section: bytes) -> set[int]:
    if len(section) < 12:
        return set()
    end = 3 + (((section[1] & 0x0F) << 8) | section[2]) - 4
    pids = set()
    for offset in range(8, max(8, end), 4):
        if offset + 4 > len(section):
            break
        program_number = (section[offset] << 8) | section[offset + 1]
        pid = ((section[offset + 2] & 0x1F) << 8) | section[offset + 3]
        if program_number:
            pids.add(pid)
    return pids


def parse_pmt(section: bytes) -> tuple[int | None, set[int]]:
    if len(section) < 16:
        return None, set()
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    end = 3 + section_length - 4
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    offset = 12 + program_info_length
    pids = set()
    while offset + 5 <= min(end, len(section)):
        pids.add(((section[offset + 1] & 0x1F) << 8) | section[offset + 2])
        es_info_length = ((section[offset + 3] & 0x0F) << 8) | section[offset + 4]
        offset += 5 + es_info_length
    return pcr_pid, pids


def parse_cat(section: bytes) -> set[int]:
    if len(section) < 12:
        return set()
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    end = min(3 + section_length - 4, len(section))
    pids = set()
    offset = 8
    while offset + 2 <= end:
        descriptor_tag = section[offset]
        descriptor_length = section[offset + 1]
        descriptor_end = offset + 2 + descriptor_length
        if descriptor_end > end:
            break
        if descriptor_tag == 0x09 and descriptor_length >= 4:
            pids.add(((section[offset + 4] & 0x1F) << 8) | section[offset + 5])
        offset = descriptor_end
    return pids


def parse_pcr(raw: bytes) -> float:
    base = (raw[0] << 25) | (raw[1] << 17) | (raw[2] << 9) | (raw[3] << 1) | (raw[4] >> 7)
    extension = ((raw[4] & 0x01) << 8) | raw[5]
    return base / 90_000 + extension / 27_000_000


def parse_pts(payload: bytes) -> float | None:
    if len(payload) < 14 or payload[:3] != b"\x00\x00\x01":
        return None
    if (payload[7] & 0x80) == 0:
        return None
    raw = payload[9:14]
    value = ((raw[0] >> 1) & 0x07) << 30
    value |= (((raw[1] << 8) | raw[2]) >> 1) << 15
    value |= ((raw[3] << 8) | raw[4]) >> 1
    return value / 90_000


def section_number(section: bytes) -> int:
    return section[6] if len(section) > 6 else 0


def section_has_crc(section: bytes) -> bool:
    return len(section) >= 7 and bool(section[1] & 0x80)


def check_si_section(report: TR101Report, pid: int, section: bytes):
    table_id = section[0]
    if pid == 0x0010 and table_id not in {0x40, 0x41, 0x72}:
        report.set("tr101_3_1_nit_error", "PID 0x0010 contains a non-NIT/ST section")
        report.set("tr101_3_1a_nit_actual_error", "PID 0x0010 contains a non-NIT/ST section")
    elif pid == 0x0011 and table_id not in {0x42, 0x46, 0x4A, 0x72}:
        report.set("tr101_3_5_sdt_error", "PID 0x0011 contains a non-SDT/BAT/ST section")
        report.set("tr101_3_5a_sdt_actual_error", "PID 0x0011 contains a non-SDT/BAT/ST section")
    elif pid == 0x0012 and not (0x4E <= table_id <= 0x6F or table_id == 0x72):
        report.set("tr101_3_6_eit_error", "PID 0x0012 contains a non-EIT/ST section")
        report.set("tr101_3_6a_eit_actual_error", "PID 0x0012 contains a non-EIT/ST section")
    elif pid == 0x0013 and table_id not in {0x71, 0x72}:
        report.set("tr101_3_7_rst_error", "PID 0x0013 contains a non-RST/ST section")
    elif pid == 0x0014 and table_id not in {0x70, 0x72, 0x73}:
        report.set("tr101_3_8_tdt_error", "PID 0x0014 contains a non-TDT/ST/TOT section")


def check_si_repeat(report: TR101Report, pid: int, table_id: int, arrival_delta: float):
    if arrival_delta > SI_REPEAT_MIN_SECONDS:
        return
    report.set("tr101_3_2_si_repetition_error", "SI section repeated within 25 ms")
    if pid == 0x0010 and table_id == 0x40:
        report.set("tr101_3_1_nit_error", "NIT_actual section repeated within 25 ms")
        report.set("tr101_3_1a_nit_actual_error", "NIT_actual section repeated within 25 ms")
    elif pid == 0x0011 and table_id == 0x42:
        report.set("tr101_3_5_sdt_error", "SDT_actual section repeated within 25 ms")
        report.set("tr101_3_5a_sdt_actual_error", "SDT_actual section repeated within 25 ms")
    elif pid == 0x0012 and table_id == 0x4E:
        report.set("tr101_3_6_eit_error", "EIT actual P/F section repeated within 25 ms")
        report.set("tr101_3_6a_eit_actual_error", "EIT actual P/F section repeated within 25 ms")
    elif pid == 0x0013 and table_id == 0x71:
        report.set("tr101_3_7_rst_error", "RST section repeated within 25 ms")
    elif pid == 0x0014 and table_id == 0x70:
        report.set("tr101_3_8_tdt_error", "TDT section repeated within 25 ms")


def check_si_presence(
    report: TR101Report,
    seen_pids: set[int],
    si_tables_seen: dict[int, set[int]],
    si_section_counts: dict[tuple[int, int, int], int],
    sample_seconds: float | None,
):
    if not sample_seconds:
        return
    if sample_seconds >= NIT_REPEAT_SECONDS and 0x0010 in seen_pids and 0x40 not in si_tables_seen.get(0x0010, set()):
        report.set("tr101_3_1_nit_error", "NIT_actual did not occur within 10 seconds")
        report.set("tr101_3_1a_nit_actual_error", "NIT_actual did not occur within 10 seconds")
        report.set("tr101_3_2_si_repetition_error", "NIT_actual repetition outside limit")
    if sample_seconds >= SDT_EIT_REPEAT_SECONDS and 0x0011 in seen_pids and 0x42 not in si_tables_seen.get(0x0011, set()):
        report.set("tr101_3_5_sdt_error", "SDT_actual did not occur within 2 seconds")
        report.set("tr101_3_5a_sdt_actual_error", "SDT_actual did not occur within 2 seconds")
        report.set("tr101_3_2_si_repetition_error", "SDT_actual repetition outside limit")
    if sample_seconds >= SDT_EIT_REPEAT_SECONDS and 0x0012 in seen_pids and 0x4E not in si_tables_seen.get(0x0012, set()):
        report.set("tr101_3_6_eit_error", "EIT actual P/F did not occur within 2 seconds")
        report.set("tr101_3_6a_eit_actual_error", "EIT actual P/F did not occur within 2 seconds")
        report.set("tr101_3_2_si_repetition_error", "EIT actual P/F repetition outside limit")
    if sample_seconds >= TDT_REPEAT_SECONDS and 0x0014 in seen_pids and 0x70 not in si_tables_seen.get(0x0014, set()):
        report.set("tr101_3_8_tdt_error", "TDT did not occur within 30 seconds")
        report.set("tr101_3_2_si_repetition_error", "TDT repetition outside limit")

    if sample_seconds >= OTHER_SI_REPEAT_SECONDS:
        for pid, table_id, indicator in (
            (0x0010, 0x41, "tr101_3_1b_nit_other_error"),
            (0x0011, 0x46, "tr101_3_5b_sdt_other_error"),
            (0x0012, 0x4F, "tr101_3_6b_eit_other_error"),
        ):
            for (current_pid, current_table_id, _section), count in si_section_counts.items():
                if current_pid == pid and current_table_id == table_id and count < 2:
                    report.set(indicator, "Other SI section did not repeat within 10 seconds")
                    report.set("tr101_3_2_si_repetition_error", "Other SI repetition outside limit")

    for current_table_id in {0x4E, 0x4F}:
        sections = {
            section
            for pid, table_id, section in si_section_counts
            if pid == 0x0012 and table_id == current_table_id and section in {0, 1}
        }
        if sections and sections != {0, 1}:
            report.set("tr101_3_6c_eit_pf_error", "EIT P/F section 0 or 1 is present without its pair")


def mpeg_crc32(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc
