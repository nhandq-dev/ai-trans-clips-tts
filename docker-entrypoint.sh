#!/bin/sh
# Start the TTS worker with N uvicorn processes.
#
# Why: each process loads its own VieNeu model and holds its own `_INFER_LOCK`
# (a `threading.Lock`), so N processes give N concurrent syntheses. Without this
# a single long request pins the whole worker (plan/014 Phase 2, measured:
# 1 lane = 37.5 chars/s, 4 lanes = 90.2 chars/s on 10 cores).
#
# Cores are split evenly across workers to avoid oversubscription: 10 cores with
# TTS_WORKERS=4 -> 2 ONNX/OpenMP threads per worker, leaving headroom for the
# pipeline and video workers. Set OMP_NUM_THREADS to pin it explicitly.
set -e

workers="${TTS_WORKERS:-1}"
# NB: `nproc` honours OMP_NUM_THREADS, so with that variable set it reports the
# thread cap instead of the core count. `getconf`/`/proc/cpuinfo` do not.
cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 1)"
threads="${OMP_NUM_THREADS:-$(( cores / workers ))}"
[ "$threads" -lt 1 ] && threads=1

# Must be exported before onnxruntime creates its session.
export OMP_NUM_THREADS="$threads"
export ORT_NUM_THREADS="$threads"
export OPENBLAS_NUM_THREADS="$threads"
export MKL_NUM_THREADS="$threads"

echo "tts-worker: workers=$workers threads=$threads cores=$cores port=${PORT:-8004}"

exec uvicorn app.main:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8004}" \
  --workers "$workers"
