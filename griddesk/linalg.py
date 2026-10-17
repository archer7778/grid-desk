"""Tiny least-squares helpers (no numpy: this has to install and run anywhere, Pi included)."""
import math


def solve(A, b):
    """Solve A x = b (small dense system, Gaussian elimination with partial pivoting)."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            if f:
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / M[r][r]
    return x


def robust_fit(X, y, ridge=1e-3, iters=3, w0=None):
    """Huber-weighted least squares with a light ridge (the intercept, column 0, is not damped).
    Returns coefficients or None."""
    n = len(X[0])
    w = list(w0) if w0 else [1.0] * len(y)
    coef = None
    scale = [max(1e-9, math.sqrt(sum(x[j] * x[j] for x in X) / len(X))) for j in range(n)]
    for _ in range(iters):
        A = [[0.0] * n for _ in range(n)]
        b = [0.0] * n
        for xi, yi, wi in zip(X, y, w):
            for i in range(n):
                v = wi * xi[i]
                b[i] += v * yi
                Ai = A[i]
                for j in range(i, n):
                    Ai[j] += v * xi[j]
        tot = sum(w)
        for i in range(n):
            for j in range(i):
                A[i][j] = A[j][i]
            if i:
                A[i][i] += ridge * tot * scale[i] ** 2
        c = solve(A, b)
        if c is None:
            break
        coef = c
        res = [yi - sum(a * f for a, f in zip(coef, xi)) for xi, yi in zip(X, y)]
        s = mad(res) or 1e-9
        w = [(w0[k] if w0 else 1.0) * (1.0 if abs(r) <= 1.5 * s else 1.5 * s / abs(r)) for k, r in enumerate(res)]
    return coef


def mad(v):
    if len(v) < 2:
        return 0.0
    s = sorted(v)
    m = s[len(s) // 2]
    d = sorted(abs(x - m) for x in v)
    return 1.4826 * d[len(d) // 2]


def quantile(sorted_v, f):
    if not sorted_v:
        return None
    i = f * (len(sorted_v) - 1)
    lo = int(i)
    hi = min(lo + 1, len(sorted_v) - 1)
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (i - lo)
