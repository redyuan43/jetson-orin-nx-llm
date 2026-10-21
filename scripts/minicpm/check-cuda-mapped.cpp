// Compare Q3_K expert kernels using device storage and CPU-written mapped storage.
// Use the actual MiniCPM expert dimensions and repeated slot updates.
#include "ggml.h"
#include "ggml-alloc.h"
#include "ggml-backend.h"
#include "ggml-cuda.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <stdexcept>
#include <vector>

static void require(bool condition, const char * message) {
    if (!condition) throw std::runtime_error(message);
}

static std::vector<float> evaluate(ggml_backend_t backend, ggml_backend_buffer_type_t type,
                                  bool mapped, int k, int m, int batches, int input_experts) {
    constexpr int experts = 16, used = 8;
    auto * wc = ggml_init({1024*1024, nullptr, true});
    auto * w = ggml_new_tensor_3d(wc, GGML_TYPE_Q3_K, k, m, experts);
    ggml_set_name(w, "expert_weights");
    auto wb = ggml_backend_alloc_ctx_tensors_from_buft(wc, type);
    require(wb != nullptr, "weight allocation failed");
    ggml_backend_buffer_set_usage(wb, GGML_BACKEND_BUFFER_USAGE_WEIGHTS);
    auto * cc = ggml_init({1024*1024, nullptr, true});
    auto * x = ggml_new_tensor_3d(cc, GGML_TYPE_F32, k, input_experts, batches);
    auto * ids = ggml_new_tensor_2d(cc, GGML_TYPE_I32, used, batches);
    auto * y = ggml_mul_mat_id(cc, w, x, ids);
    auto * graph = ggml_new_graph_custom(cc, 128, false);
    ggml_build_forward_expand(graph, y);
    auto cb = ggml_backend_alloc_ctx_tensors(cc, backend);
    require(cb != nullptr, "compute allocation failed");
    std::mt19937 rng(20261021);
    std::uniform_real_distribution<float> dist(-0.5f, 0.5f);
    std::vector<float> wf((size_t) k*m*experts), xf(ggml_nelements(x));
    std::vector<unsigned char> quant(ggml_nbytes(w));
    std::vector<int32_t> idf(used*batches);
    std::vector<float> result;
    for (int step = 0; step < 3; ++step) {
        for (auto & v : wf) v = dist(rng);
        for (auto & v : xf) v = dist(rng);
        for (int i = 0; i < used*batches; ++i) idf[i] = (i*3 + step*5) % experts;
        require(ggml_quantize_chunk(GGML_TYPE_Q3_K, wf.data(), quant.data(), 0,
                                    m*experts, k, nullptr) == quant.size(), "quantization size");
        if (mapped) {
            std::memcpy(w->data, quant.data(), quant.size());
        } else {
            ggml_backend_tensor_set(w, quant.data(), 0, quant.size());
        }
        ggml_backend_tensor_set(x, xf.data(), 0, ggml_nbytes(x));
        ggml_backend_tensor_set(ids, idf.data(), 0, ggml_nbytes(ids));
        require(ggml_backend_graph_compute(backend, graph) == GGML_STATUS_SUCCESS, "graph compute");
        const size_t offset = result.size();
        result.resize(offset + ggml_nelements(y));
        ggml_backend_tensor_get(y, result.data() + offset, 0, ggml_nbytes(y));
    }
    ggml_backend_buffer_free(cb);
    ggml_backend_buffer_free(wb);
    ggml_free(cc);
    ggml_free(wc);
    return result;
}

int main() {
    setenv("GGML_CUDA_LES_MAPPED", "1", 1);
    auto backend = ggml_backend_cuda_init(0);
    require(backend != nullptr, "CUDA backend");
    auto dev = ggml_backend_get_device(backend);
    auto reg = ggml_backend_dev_backend_reg(dev);
    auto fn = (ggml_backend_buffer_type_t (*)(size_t))
        ggml_backend_reg_get_proc_address(reg, "ggml_backend_dev_buffer_type_alloc_only");
    require(fn != nullptr, "mapped extension missing");
    auto mapped = fn(0);
    require(mapped && ggml_backend_buft_is_host_addressable(mapped) && !ggml_backend_buft_is_host(mapped),
            "mapped buffer capabilities");
    auto device = ggml_backend_dev_buffer_type(dev);
    bool pass = true;
    for (int batches : {1, 2, 4, 8}) {
        for (bool down : {false, true}) {
            const int k = down ? 512 : 2048, m = down ? 2048 : 512;
            auto a = evaluate(backend, device, false, k, m, batches, down ? 8 : 1);
            auto b = evaluate(backend, mapped, true, k, m, batches, down ? 8 : 1);
            require(a.size() == b.size(), "output shape");
            double max_error = 0, squared_error = 0, squared_ref = 0;
            for (size_t i = 0; i < a.size(); ++i) {
                require(std::isfinite(a[i]) && std::isfinite(b[i]), "nonfinite output");
                double diff = double(a[i]) - b[i];
                max_error = std::max(max_error, std::abs(diff));
                squared_error += diff*diff;
                squared_ref += double(a[i])*a[i];
            }
            const double nrmse = std::sqrt(squared_error/std::max(squared_ref, 1e-30));
            const bool ok = nrmse <= 1e-6;
            pass &= ok;
            std::printf("%s k=%d m=%d batch=%d values=%zu max_abs=%.9g nrmse=%.9g\n",
                        ok ? "PASS" : "FAIL", k, m, batches, a.size(), max_error, nrmse);
        }
    }
    ggml_backend_free(backend);
    return pass ? 0 : 1;
}
