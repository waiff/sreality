import os, time, numpy as np
# ViT-B/16 GEMM shapes at 768 px (2305 tokens incl. cls; DINOv3 adds 4 registers -> 2309) and attention.
def bench(n_tok, reps=3):
    d = 768
    x = np.random.rand(n_tok, d).astype(np.float32)
    wqkv = np.random.rand(d, 3 * d).astype(np.float32); wo = np.random.rand(d, d).astype(np.float32)
    w1 = np.random.rand(d, 4 * d).astype(np.float32); w2 = np.random.rand(4 * d, d).astype(np.float32)
    best = 1e9
    for _ in range(reps):
        t = time.perf_counter()
        q = x @ wqkv
        h = 12; dh = 64
        qq = q[:, :d].reshape(n_tok, h, dh).transpose(1, 0, 2)
        kk = q[:, d:2*d].reshape(n_tok, h, dh).transpose(1, 2, 0)
        vv = q[:, 2*d:].reshape(n_tok, h, dh).transpose(1, 0, 2)
        att = qq @ kk
        att = np.exp(att - att.max(-1, keepdims=True)); att /= att.sum(-1, keepdims=True)
        o = (att @ vv).transpose(1, 0, 2).reshape(n_tok, d) @ wo
        y = np.maximum(o @ w1, 0) @ w2
        best = min(best, time.perf_counter() - t)
    macs = n_tok * 12 * d * d + 2 * n_tok * n_tok * d
    return best, macs
for res in (224, 512, 768):
    n = (res // 16) ** 2 + 5
    t, macs = bench(n)
    per_img = t * 12
    print(f"res {res} tokens {n} layer {t*1000:.1f} ms  -> ViT-B/16 forward {per_img:.2f} s/img  {2*macs*12/1e9:.0f} GFLOP  {2*macs/t/1e9:.1f} GFLOP/s threads={os.environ.get('OPENBLAS_NUM_THREADS')}")
