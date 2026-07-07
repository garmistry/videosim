import unittest

from videosim.tr101 import analyze_ts, mpeg_crc32


PAT_PID = 0
PMT_PID = 100
VIDEO_PID = 200
AUDIO_PID = 201


def with_crc(section_without_crc):
    crc = mpeg_crc32(section_without_crc)
    return section_without_crc + crc.to_bytes(4, "big")


def pat_section(pmt_pid=PMT_PID):
    return with_crc(
        bytes(
            [
                0x00,
                0xB0,
                13,
                0x00,
                0x01,
                0xC1,
                0x00,
                0x00,
                0x00,
                0x01,
                0xE0 | (pmt_pid >> 8),
                pmt_pid & 0xFF,
            ]
        )
    )


def pmt_section(video_pid=VIDEO_PID, audio_pid=AUDIO_PID):
    body = [
        0x02,
        0xB0,
        23,
        0x00,
        0x01,
        0xC1,
        0x00,
        0x00,
        0xE0 | (video_pid >> 8),
        video_pid & 0xFF,
        0xF0,
        0x00,
        0x1B,
        0xE0 | (video_pid >> 8),
        video_pid & 0xFF,
        0xF0,
        0x00,
        0x0F,
        0xE0 | (audio_pid >> 8),
        audio_pid & 0xFF,
        0xF0,
        0x00,
    ]
    return with_crc(bytes(body))


def si_section(table_id, section_number=0):
    return with_crc(bytes([table_id, 0xB0, 9, 0x00, 0x01, 0xC1, section_number, 0x00]))


def packet(pid, payload=b"", cc=0, pusi=False, transport_error=False, scrambled=False, adaptation=b""):
    afc = 3 if adaptation else 1
    header = bytes(
        [
            0x47,
            (0x80 if transport_error else 0) | (0x40 if pusi else 0) | ((pid >> 8) & 0x1F),
            pid & 0xFF,
            (0x80 if scrambled else 0) | (afc << 4) | cc,
        ]
    )
    body = header
    if adaptation:
        body += bytes([len(adaptation)]) + adaptation
    body += payload
    return body + (b"\xFF" * (188 - len(body)))


def section_packet(pid, section, cc=0, **kwargs):
    return packet(pid, b"\x00" + section, cc=cc, pusi=True, **kwargs)


def pcr_adaptation(seconds, discontinuity=False):
    base = int(seconds * 90_000)
    raw = bytes(
        [
            (base >> 25) & 0xFF,
            (base >> 17) & 0xFF,
            (base >> 9) & 0xFF,
            (base >> 1) & 0xFF,
            ((base & 1) << 7) | 0x7E,
            0x00,
        ]
    )
    return bytes([(0x80 if discontinuity else 0) | 0x10]) + raw


def pts_bytes(seconds):
    value = int(seconds * 90_000)
    return bytes(
        [
            0x20 | (((value >> 30) & 0x07) << 1) | 1,
            (value >> 22) & 0xFF,
            (((value >> 15) & 0x7F) << 1) | 1,
            (value >> 7) & 0xFF,
            ((value & 0x7F) << 1) | 1,
        ]
    )


def pes_packet(pid, seconds, cc):
    payload = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + pts_bytes(seconds)
    return packet(pid, payload, cc=cc, pusi=True)


def valid_ts():
    return b"".join(
        [
            section_packet(PAT_PID, pat_section(), cc=0),
            section_packet(PMT_PID, pmt_section(), cc=0),
            packet(VIDEO_PID, b"video", cc=0, adaptation=pcr_adaptation(0.00)),
            pes_packet(VIDEO_PID, 0.00, cc=1),
            packet(VIDEO_PID, b"video", cc=2, adaptation=pcr_adaptation(0.02)),
            pes_packet(VIDEO_PID, 0.04, cc=3),
            packet(AUDIO_PID, b"audio", cc=0),
        ]
    )


class TR101Test(unittest.TestCase):
    def test_valid_ts_has_no_tr101_errors(self):
        report = analyze_ts(valid_ts(), sample_seconds=0.07)

        self.assertFalse(any(report.indicators.values()), report.messages)

    def test_detects_sync_transport_and_crc_errors(self):
        bad_pat = bytearray(pat_section())
        bad_pat[-1] ^= 0xFF
        data = b"".join(
            [
                b"\x00" * 188,
                b"\x00" * 188,
                section_packet(PAT_PID, bytes(bad_pat), transport_error=True),
            ]
        )

        report = analyze_ts(data)

        self.assertTrue(report.indicators["tr101_1_1_ts_sync_loss"])
        self.assertTrue(report.indicators["tr101_1_2_sync_byte_error"])
        self.assertTrue(report.indicators["tr101_2_1_transport_error"])
        self.assertTrue(report.indicators["tr101_2_2_crc_error"])

    def test_detects_continuity_missing_pid_and_pcr_pts_errors(self):
        data = b"".join(
            [
                section_packet(PAT_PID, pat_section(), cc=0),
                section_packet(PMT_PID, pmt_section(), cc=0),
                packet(VIDEO_PID, b"video", cc=0, adaptation=pcr_adaptation(0.00)),
                packet(VIDEO_PID, b"video", cc=0),
                packet(VIDEO_PID, b"video", cc=0),
                packet(VIDEO_PID, b"video", cc=1, adaptation=pcr_adaptation(0.20)),
                pes_packet(VIDEO_PID, 0.00, cc=2),
                pes_packet(VIDEO_PID, 0.80, cc=3),
            ]
        )

        report = analyze_ts(data, sample_seconds=0.08)

        self.assertTrue(report.indicators["tr101_1_4_continuity_count_error"])
        self.assertTrue(report.indicators["tr101_1_6_pid_error"])
        self.assertTrue(report.indicators["tr101_2_3_pcr_error"])
        self.assertTrue(report.indicators["tr101_2_3a_pcr_repetition_error"])
        self.assertTrue(report.indicators["tr101_2_3b_pcr_discontinuity_indicator_error"])
        self.assertTrue(report.indicators["tr101_2_4_pcr_accuracy_error"])
        self.assertTrue(report.indicators["tr101_2_5_pts_error"])

    def test_detects_priority_3_unreferenced_pid(self):
        report = analyze_ts(valid_ts() + packet(300, b"private"), sample_seconds=1.0)

        self.assertTrue(report.indicators["tr101_3_4_unreferenced_pid"])
        self.assertTrue(report.indicators["tr101_3_4a_unreferenced_pid"])

    def test_detects_priority_3_si_syntax_repetition_and_pairing(self):
        data = b"".join(
            [
                valid_ts(),
                section_packet(0x0011, si_section(0x40), cc=0),
                section_packet(0x0012, si_section(0x4E, section_number=0), cc=0),
                section_packet(0x0013, si_section(0x71), cc=0),
                section_packet(0x0013, si_section(0x71), cc=1),
            ]
        )

        report = analyze_ts(data, sample_seconds=0.1)

        self.assertTrue(report.indicators["tr101_3_5_sdt_error"])
        self.assertTrue(report.indicators["tr101_3_5a_sdt_actual_error"])
        self.assertTrue(report.indicators["tr101_3_6c_eit_pf_error"])
        self.assertTrue(report.indicators["tr101_3_7_rst_error"])
        self.assertTrue(report.indicators["tr101_3_2_si_repetition_error"])

    def test_detects_priority_3_observed_si_presence_gap(self):
        report = analyze_ts(valid_ts() + packet(0x0014, b""), sample_seconds=31)

        self.assertTrue(report.indicators["tr101_3_8_tdt_error"])
        self.assertTrue(report.indicators["tr101_3_2_si_repetition_error"])


if __name__ == "__main__":
    unittest.main()
