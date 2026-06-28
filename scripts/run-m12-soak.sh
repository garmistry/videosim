#!/usr/bin/env bash
set -euo pipefail

REPORT_DIR="${REPORT_DIR:-reports/m12-soak/$(date -u +%Y%m%dT%H%M%SZ)}"
NORMAL_DURATION_SECONDS="${NORMAL_DURATION_SECONDS:-86400}"
OUTAGE_DURATION_SECONDS="${OUTAGE_DURATION_SECONDS:-3600}"
VALIDATION_INTERVAL_SECONDS="${VALIDATION_INTERVAL_SECONDS:-900}"
WIDTH="${WIDTH:-320}"
HEIGHT="${HEIGHT:-180}"
FRAMERATE="${FRAMERATE:-10}"
STARTUP_SECONDS="${STARTUP_SECONDS:-4}"

mkdir -p "${REPORT_DIR}"

run_soak() {
  local name="$1"
  local profile="$2"
  local port="$3"
  local duration="$4"

  echo "Running ${name} soak for ${duration}s on port ${port}"
  python3 -m videosim soak \
    --profile "${profile}" \
    --port "${port}" \
    --width "${WIDTH}" \
    --height "${HEIGHT}" \
    --framerate "${FRAMERATE}" \
    --duration-seconds "${duration}" \
    --validation-interval-seconds "${VALIDATION_INTERVAL_SECONDS}" \
    --startup-seconds "${STARTUP_SECONDS}" \
    --json | tee "${REPORT_DIR}/${name}.json"
}

run_soak normal profiles/srt-normal.yaml 9100 "${NORMAL_DURATION_SECONDS}"
run_soak audio_only profiles/srt-audio-only.yaml 9101 "${OUTAGE_DURATION_SECONDS}"
run_soak video_only profiles/srt-video-only.yaml 9102 "${OUTAGE_DURATION_SECONDS}"
run_soak no_captions profiles/srt-no-captions.yaml 9103 "${OUTAGE_DURATION_SECONDS}"
run_soak black_video profiles/srt-black-video.yaml 9104 "${OUTAGE_DURATION_SECONDS}"
run_soak frozen_video profiles/srt-frozen-video.yaml 9105 "${OUTAGE_DURATION_SECONDS}"

python3 -m videosim soak-check --report-dir "${REPORT_DIR}" --json | tee "${REPORT_DIR}/summary.json"

echo "M12 soak reports written to ${REPORT_DIR}"
