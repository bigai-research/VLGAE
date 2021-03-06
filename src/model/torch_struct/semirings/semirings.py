from abc import ABC, abstractmethod
from functools import reduce
from typing import List, Union

import torch
from torch import Tensor

has_genbmm = False
try:
    import genbmm

    has_genbmm = True
except ImportError:
    pass

NEGINF = -1e12


class Semiring(ABC):
    """
    Base semiring class.

    Based on description in:

    * Semiring parsing :cite:`goodman1999semiring`

    """
    zero = None

    @classmethod
    def size(cls) -> int:
        'Additional *ssize* first dimension needed.'
        return 1

    @classmethod
    def plus(cls, a: Tensor, b: Tensor) -> Tensor:
        """Sum over last dim of two tensor"""
        return cls.sum(torch.stack([a, b], dim=-1))

    @staticmethod
    @abstractmethod
    def sum(xs: Tensor, dim: int = -1) -> Tensor:
        'Sum over *dim* of tensor.'
        pass

    @staticmethod
    @abstractmethod
    def prod(a: Tensor, dim: int = -1) -> Tensor:
        """Product of all elements in the input tensor on dim."""
        pass

    @classmethod
    def matmul(cls, a: Tensor, b: Tensor) -> Tensor:
        'Generalized matmul.'
        a = a.unsqueeze(-1)  # ~ * n * n * 1
        b = b.unsqueeze(-3)  # ~ * 1 * n * n
        c = cls.times(a, b)
        c = cls.sum(c.transpose(-2, -1))
        return c

    @classmethod
    def dot(cls, a: Tensor, b: Tensor) -> Tensor:
        'Dot product along last dim.'
        a = a.unsqueeze(-2)
        b = b.unsqueeze(-1)
        return cls.matmul(a, b).squeeze(-1).squeeze(-1)

    @staticmethod
    @abstractmethod
    def mul(a: Tensor, b: Tensor) -> Tensor:
        """Element-wisely multiply two tensors"""
        pass

    @classmethod
    def times(cls, *ls: Tensor) -> Tensor:
        'Element-wisely multiply a list of tensors together'
        return reduce(cls.mul, ls)

    @classmethod
    def convert(cls, potentials: Union[Tensor, List[Tensor]]) -> Tensor:
        'Convert to semiring by adding an extra first dimension.'
        return potentials.unsqueeze(0)

    @classmethod
    def unconvert(cls, potentials: Tensor) -> Tensor:
        'Unconvert from semiring by removing extra first dimension.'
        return potentials.squeeze(0)

    @staticmethod
    @abstractmethod
    def zero_(xs: Tensor) -> Tensor:
        'Fill *ssize x ...* tensor with additive identity.'
        pass

    @classmethod
    def zero_mask_(cls, xs: Tensor, mask: Tensor) -> None:
        'Fill *ssize x ...* tensor with additive identity.'
        xs.masked_fill_(mask.unsqueeze(0), cls.zero)

    @staticmethod
    @abstractmethod
    def one_(xs: Tensor) -> Tensor:
        'Fill *ssize x ...* tensor with multiplicative identity.'
        pass


class _Base(Semiring):
    zero = 0

    @staticmethod
    def mul(a, b):
        return torch.mul(a, b)

    @staticmethod
    def prod(a, dim=-1):
        return torch.prod(a, dim=dim)

    @staticmethod
    def zero_(xs):
        return xs.fill_(0)

    @staticmethod
    def one_(xs):
        return xs.fill_(1)


class _BaseLog(Semiring):
    zero = NEGINF

    @staticmethod
    def sum(xs, dim=-1):
        return torch.logsumexp(xs, dim=dim)

    @staticmethod
    def mul(a, b):
        return a + b

    @staticmethod
    def zero_(xs):
        return xs.fill_(NEGINF)

    @staticmethod
    def one_(xs):
        return xs.fill_(0.0)

    @staticmethod
    def prod(a, dim=-1):
        return torch.sum(a, dim=dim)


class StdSemiring(_Base):
    """
    Implements the counting semiring (+, *, 0, 1).
    """
    @staticmethod
    def sum(xs, dim=-1):
        return torch.sum(xs, dim=dim)

    @classmethod
    def matmul(cls, a, b, dims=1):
        """
        Dot product along last dim.

        (Faster than calling sum and times.)
        """

        if has_genbmm and isinstance(a, genbmm.BandedMatrix):
            return b.multiply(a.transpose())
        else:
            return torch.matmul(a, b)


class LogSemiring(_BaseLog):
    """
    Implements the log-space semiring (logsumexp, +, -inf, 0).

    Gradients give marginals.
    """
    @classmethod
    def matmul(cls, a, b):
        if has_genbmm and isinstance(a, genbmm.BandedMatrix):
            return b.multiply_log(a.transpose())
        else:
            return _BaseLog.matmul(a, b)


class MaxSemiring(_BaseLog):
    """
    Implements the max semiring (max, +, -inf, 0).

    Gradients give argmax.
    """
    @classmethod
    def matmul(cls, a, b):
        if has_genbmm and isinstance(a, genbmm.BandedMatrix):
            return b.multiply_max(a.transpose())
        else:
            return super(MaxSemiring, cls).matmul(a, b)

    @staticmethod
    def sum(xs, dim=-1):
        return torch.max(xs, dim=dim)[0]

    @staticmethod
    def sparse_sum(xs, dim=-1):
        m, a = torch.max(xs, dim=dim)
        return m, (torch.zeros(a.shape).long(), a)


def KMaxSemiring(k):
    """
    Implements the k-max semiring (kmax, +, [-inf, -inf..], [0, -inf, ...]).

    Gradients give k-argmax.
    """
    class KMaxSemiring(_BaseLog):
        @classmethod
        def size(cls):
            return k

        @classmethod
        def convert(cls, orig_potentials):
            potentials = torch.zeros(
                (k, ) + orig_potentials.shape,
                dtype=orig_potentials.dtype,
                device=orig_potentials.device,
            )
            cls.zero_(potentials)
            potentials[0] = orig_potentials
            return potentials

        @classmethod
        def one_(cls, xs):
            cls.zero_(xs)
            xs[0].fill_(0)
            return xs

        @classmethod
        def unconvert(cls, potentials):
            return potentials[0]

        @staticmethod
        def sum(xs, dim=-1):
            if dim == -1:
                xs = xs.permute(tuple(range(1, xs.dim())) + (0, ))
                xs = xs.contiguous().view(xs.shape[:-2] + (-1, ))
                xs = torch.topk(xs, k, dim=-1)[0]
                xs = xs.permute((xs.dim() - 1, ) + tuple(range(0, xs.dim() - 1)))
                assert xs.shape[0] == k
                return xs
            assert False

        @staticmethod
        def sparse_sum(xs, dim=-1):
            if dim == -1:
                xs = xs.permute(tuple(range(1, xs.dim())) + (0, ))
                xs = xs.contiguous().view(xs.shape[:-2] + (-1, ))
                xs, xs2 = torch.topk(xs, k, dim=-1)
                xs = xs.permute((xs.dim() - 1, ) + tuple(range(0, xs.dim() - 1)))
                xs2 = xs2.permute((xs.dim() - 1, ) + tuple(range(0, xs.dim() - 1)))
                assert xs.shape[0] == k
                return xs, (xs2 % k, xs2 // k)
            assert False

        @staticmethod
        def mul(a, b):
            a = a.view((k, 1) + a.shape[1:])
            b = b.view((1, k) + b.shape[1:])
            c = a + b
            c = c.contiguous().view((k * k, ) + c.shape[2:])
            ret = torch.topk(c, k, 0)[0]
            assert ret.shape[0] == k
            return ret

    return KMaxSemiring


class KLDivergenceSemiring(Semiring):
    """
    Implements an KL-divergence semiring.

    Computes both the log-values of two distributions and the running KL divergence between two distributions.

    Based on descriptions in:

    * Parameter estimation for probabilistic finite-state transducers :cite:`eisner2002parameter`
    * First-and second-order expectation semirings with applications to minimum-risk training on translation forests :cite:`li2009first`
    * Sample Selection for Statistical Grammar Induction :cite:`hwa2000samplesf`
    """
    zero = 0

    @classmethod
    def size(cls):
        """inside of p; inside of q; mid-result"""
        return 3

    @classmethod
    def convert(cls, xs):
        values = torch.zeros((3, ) + xs[0].shape).type_as(xs[0])
        values[0] = xs[0]
        values[1] = xs[1]
        values[2] = 0
        return values

    @classmethod
    def unconvert(cls, xs):
        return xs[-1]

    @staticmethod
    def sum(xs, dim=-1):
        assert dim != 0
        d = dim - 1 if dim > 0 else dim
        part_p = torch.logsumexp(xs[0], dim=d)
        part_q = torch.logsumexp(xs[1], dim=d)
        log_sm_p = xs[0] - part_p.unsqueeze(d)
        log_sm_q = xs[1] - part_q.unsqueeze(d)
        sm_p = log_sm_p.exp()
        return torch.stack((part_p, part_q, torch.sum(xs[2].mul(sm_p) - log_sm_q.mul(sm_p) + log_sm_p.mul(sm_p),
                                                      dim=d)))

    @staticmethod
    def mul(a, b):
        return a + b

    @classmethod
    def prod(cls, xs, dim=-1):
        return xs.sum(dim)

    @classmethod
    def zero_mask_(cls, xs, mask):
        'Fill *ssize x ...* tensor with additive identity.'
        xs[0].masked_fill_(mask, NEGINF)
        xs[1].masked_fill_(mask, NEGINF)
        xs[2].masked_fill_(mask, 0)

    @staticmethod
    def zero_(xs):
        xs[0].fill_(NEGINF)
        xs[1].fill_(NEGINF)
        xs[2].fill_(0)
        return xs

    @staticmethod
    def one_(xs):
        xs[0].fill_(0)
        xs[1].fill_(0)
        xs[2].fill_(0)
        return xs


class CrossEntropySemiring(Semiring):
    """
    Implements an cross-entropy expectation semiring.

    Computes both the log-values of two distributions and the running cross entropy between two distributions.

    Based on descriptions in:

    * Parameter estimation for probabilistic finite-state transducers :cite:`eisner2002parameter`
    * First-and second-order expectation semirings with applications to minimum-risk training on translation forests :cite:`li2009first`
    * Sample Selection for Statistical Grammar Induction :cite:`hwa2000samplesf`
    """

    zero = (NEGINF, NEGINF, 0)

    @classmethod
    def size(cls):
        """inside of p; inside of q; mid-result"""
        return 3

    @classmethod
    def convert(cls, xs):
        values = torch.zeros((3, ) + xs[0].shape).type_as(xs[0])
        values[0] = xs[0]
        values[1] = xs[1]
        values[2] = 0
        return values

    @classmethod
    def unconvert(cls, xs):
        return xs[-1]

    @classmethod
    def sum(cls, xs, dim=-1):
        assert dim != 0
        d = dim - 1 if dim > 0 else dim
        part_p = torch.logsumexp(xs[0], dim=d)
        part_q = torch.logsumexp(xs[1], dim=d)
        log_sm_p = xs[0] - part_p.unsqueeze(d)
        log_sm_q = xs[1] - part_q.unsqueeze(d)
        sm_p = log_sm_p.exp()
        return torch.stack((part_p, part_q, torch.sum(xs[2].mul(sm_p) - log_sm_q.mul(sm_p), dim=d)))

    @classmethod
    def mul(cls, a, b):
        return a + b

    @classmethod
    def prod(cls, xs, dim=-1):
        return xs.sum(dim)

    @classmethod
    def zero_mask_(cls, xs, mask):
        'Fill *ssize x ...* tensor with additive identity.'
        xs[0].masked_fill_(mask, NEGINF)
        xs[1].masked_fill_(mask, NEGINF)
        xs[2].masked_fill_(mask, 0)

    @staticmethod
    def zero_(xs):
        xs[0].fill_(NEGINF)
        xs[1].fill_(NEGINF)
        xs[2].fill_(0)
        return xs

    @staticmethod
    def one_(xs):
        xs[0].fill_(0)
        xs[1].fill_(0)
        xs[2].fill_(0)
        return xs


class EntropySemiring(Semiring):
    """
    Implements an entropy expectation semiring.

    Computes both the log-values and the running distributional entropy.

    Based on descriptions in:

    * Parameter estimation for probabilistic finite-state transducers :cite:`eisner2002parameter`
    * First-and second-order expectation semirings with applications to minimum-risk training on translation forests :cite:`li2009first`
    * Sample Selection for Statistical Grammar Induction :cite:`hwa2000samplesf`
    """

    zero = (NEGINF, 0)

    @classmethod
    def size(cls):
        """inside; mid-result"""
        return 2

    @classmethod
    def convert(cls, xs):
        values = torch.zeros((2, ) + xs.shape).type_as(xs)
        values[0] = xs
        values[1] = 0
        return values

    @classmethod
    def unconvert(cls, xs):
        return xs[1]

    @staticmethod
    def sum(xs, dim=-1):
        assert dim != 0
        d = dim - 1 if dim > 0 else dim
        part = torch.logsumexp(xs[0], dim=d)
        log_sm = xs[0] - part.unsqueeze(d)
        sm = log_sm.exp()
        return torch.stack((part, torch.sum(xs[1].mul(sm) - log_sm.mul(sm), dim=d)))

    @staticmethod
    def mul(a, b):
        return a + b

    @classmethod
    def prod(cls, xs, dim=-1):
        return xs.sum(dim)

    @classmethod
    def zero_mask_(cls, xs, mask):
        'Fill *ssize x ...* tensor with additive identity.'
        xs[0].masked_fill_(mask, NEGINF)
        xs[1].masked_fill_(mask, 0)

    @staticmethod
    def zero_(xs):
        xs[0].fill_(NEGINF)
        xs[1].fill_(0)
        return xs

    @staticmethod
    def one_(xs):
        xs[0].fill_(0)
        xs[1].fill_(0)
        return xs


def TempMax(alpha):
    class _TempMax(_BaseLog):
        """
        Implements a max forward, hot softmax backward.
        """
        @staticmethod
        def sum(xs, dim=-1):
            pass

        @staticmethod
        def sparse_sum(xs, dim=-1):
            m, _ = torch.max(xs, dim=dim)
            a = torch.softmax(alpha * xs, dim)
            return m, (torch.zeros(a.shape[:-1]).long(), a)

    return _TempMax


class RiskSemiring(Semiring):

    zero = (NEGINF, 0, 0)

    @classmethod
    def size(cls):
        return 3

    @classmethod
    def convert(cls, xs):
        values = torch.zeros((3, ) + xs[0].shape).type_as(xs[0])
        values[0] = xs[0]
        values[1] = xs[1]
        values[2] = 0
        return values

    @classmethod
    def unconvert(cls, xs):
        return xs[-1]

    @classmethod
    def sum(cls, xs, dim=-1):
        assert dim != 0
        d = dim - 1 if dim > 0 else dim
        part_p = torch.logsumexp(xs[0], dim=d)
        log_sm_p = xs[0] - part_p.unsqueeze(d)
        sm_p = log_sm_p.exp()
        return torch.stack((part_p, torch.zeros_like(part_p), torch.sum((xs[1] + xs[2]).mul(sm_p), dim=d)))

    @classmethod
    def mul(cls, a, b):
        return a + b

    @classmethod
    def prod(cls, xs, dim=-1):
        return xs.sum(dim)

    @classmethod
    def zero_mask_(cls, xs, mask):
        'Fill *ssize x ...* tensor with additive identity.'
        xs[0].masked_fill_(mask, NEGINF)
        xs[1].masked_fill_(mask, 0)
        xs[2].masked_fill_(mask, 0)

    @staticmethod
    def zero_(xs):
        xs[0].fill_(NEGINF)
        xs[1].fill_(0)
        xs[2].fill_(0)
        return xs

    @staticmethod
    def one_(xs):
        xs[0].fill_(0)
        xs[1].fill_(0)
        xs[2].fill_(0)
        return xs
