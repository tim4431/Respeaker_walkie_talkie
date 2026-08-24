# libfvad (WebRTC VAD) vendored as an ESP-IDF component.
# Upstream: https://github.com/dpirch/libfvad (BSD-3-Clause, see LICENSE).
idf_component_register(
    SRCS
        "src/fvad.c"
        "src/signal_processing/division_operations.c"
        "src/signal_processing/energy.c"
        "src/signal_processing/get_scaling_square.c"
        "src/signal_processing/resample_48khz.c"
        "src/signal_processing/resample_by_2_internal.c"
        "src/signal_processing/resample_fractional.c"
        "src/signal_processing/spl_inl.c"
        "src/vad/vad_core.c"
        "src/vad/vad_filterbank.c"
        "src/vad/vad_gmm.c"
        "src/vad/vad_sp.c"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "src")
