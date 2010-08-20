import sys, pickle

import stats

try:
    from scipy.stats import ttest_1samp
except ImportError:
    from math import pi
    from ctypes import CDLL, c_double
    libc = CDLL('libc.dylib')
    gamma = libc.gamma
    gamma.argtypes = [c_double]
    gamma.restype = c_double
    def ttest_1samp(a, popmean):
        t = (stats.mean(a) - popmean) / (stats.stddev(a) / len(a) ** 0.5)
        v = len(a) - 1.0
        p = gamma((v + 1) / 2) / ((v * pi) ** 0.5 * gamma(v / 2)) * (1 + t ** 2 / v) ** (-(v + 1) / 2)
        return (
            [t, None], 
            [p, None])


def select(statistics, benchmark, parameter, statistic):
    for stat, samples in statistics[benchmark][int(parameter)].iteritems():
        if stat.name == statistic:
            return (stat, samples)
    raise ValueError("Unknown statistic %r" % (statistic,))


def main():
    first = pickle.load(file(sys.argv[1]))
    second = pickle.load(file(sys.argv[2]))

    stat, first = select(first, *sys.argv[3:])
    stat, second = select(second, *sys.argv[3:])

    fmean = stats.mean(first)
    p = 1 - ttest_1samp(second, fmean)[1][0]
    if p >= 0.95:
        # rejected the null hypothesis
        print sys.argv[1], 'mean of', fmean, 'differs from', sys.argv[2], 'mean of', stats.mean(second), '(%2.0f%%)' % (p * 100,)
    else:
        # failed to reject the null hypothesis
        print 'cannot prove means differ (%2.0f%%)' % (p * 100,)