"""实盘组合模拟: 起始日建仓 + 每季度最后交易日收盘再平衡。

费率: max(成交额 * 佣金率, 单笔最低佣金)。
"""

import pandas as pd


def _fee(notional, comm, min_comm):
    return max(notional * comm, min_comm)


def _quarter_ends(start_year, end_year):
    ends = []
    for year in range(start_year, end_year + 1):
        for month in (3, 6, 9, 12):
            ends.append(pd.Timestamp(f'{year}-{month:02d}-28') + pd.offsets.MonthEnd(0))
    return ends


def run_portfolio(closes, weights, start='2026-01-05', initial=100000,
                  comm=0.00005, min_comm=0.5):
    """closes: {code: Series(close)}; weights: [(code, target_weight)]"""
    if abs(sum(w for _, w in weights) - 1.0) > 1e-9:
        raise ValueError('权重之和必须为1')

    idx = sorted(set().union(*[set(s.index) for s in closes.values()]))
    idx = [d for d in idx if d >= pd.Timestamp(start)]
    if not idx:
        raise ValueError('起始日之后没有任何行情数据')
    common = pd.DatetimeIndex(idx)
    aligned = {code: closes[code].reindex(common).ffill() for code, _ in weights}
    if any(pd.isna(aligned[code].iloc[0]) for code, _ in weights):
        raise ValueError('起始日缺少某只标的的价格')

    rb_dates = set()
    for qe in _quarter_ends(pd.Timestamp(start).year, common[-1].year):
        if qe > common[-1]:
            continue  # 季度末尚未到来, 不能提前触发
        candidates = [d for d in common if d <= qe]
        if candidates and candidates[-1] > common[0]:
            rb_dates.add(candidates[-1])

    def fee(v):
        return _fee(v, comm, min_comm)

    cash = float(initial)
    shares = {}
    for code, weight in weights:
        p0 = float(aligned[code].iloc[0])
        target = initial * weight
        s = int((target - fee(target)) / p0 / 100) * 100
        cost = s * p0 + fee(s * p0)
        if cost > cash:
            s = int((cash - fee(cash)) / p0 / 100) * 100
            cost = s * p0 + fee(s * p0)
        cash -= cost
        shares[code] = s

    def value_at(d):
        return cash + sum(shares[code] * float(aligned[code].loc[d]) for code, _ in weights)

    equity = []
    rebalances = []
    fees_paid = 0.0

    for d in common:
        if d in rb_dates:
            v_before = value_at(d)
            total_fee = 0.0
            shares_before = dict(shares)
            targets = {}
            for code, weight in weights:
                p = float(aligned[code].loc[d])
                t = v_before * weight
                targets[code] = int((t - fee(t)) / p / 100) * 100

            trades = []
            for code, _ in weights:
                cur, tgt = shares[code], targets[code]
                if cur > tgt:
                    p = float(aligned[code].loc[d])
                    n = cur - tgt
                    amount = n * p
                    f = fee(amount)
                    total_fee += f
                    cash += amount - f
                    shares[code] = tgt
                    trades.append({
                        'code': code, 'action': '卖出', 'shares': n,
                        'price': round(p, 3), 'amount': round(amount, 2),
                        'fee': round(f, 2),
                    })

            buy_needs = []
            for code, _ in weights:
                cur, tgt = shares[code], targets[code]
                if cur < tgt:
                    p = float(aligned[code].loc[d])
                    buy_needs.append((code, cur, tgt, p, (tgt - cur) * p))
            # 现金不足以覆盖全部买入目标时, 优先满足偏离目标权重最多(金额最大)的资产,
            # 避免固定按 ASSETS 顺序导致排在后面的资产总是被牺牲
            buy_needs.sort(key=lambda item: -item[4])

            for code, cur, tgt, p, _ in buy_needs:
                need = tgt - cur
                amount = need * p
                f = fee(amount)
                if cash >= amount + f:
                    cash -= amount + f
                    total_fee += f
                    shares[code] = tgt
                    trades.append({
                        'code': code, 'action': '买入', 'shares': need,
                        'price': round(p, 3), 'amount': round(amount, 2),
                        'fee': round(f, 2),
                    })
                else:
                    s = int((cash - fee(cash)) / p / 100) * 100
                    if s > 0:
                        amount = s * p
                        f = fee(amount)
                        total_fee += f
                        cash -= amount + f
                        shares[code] = cur + s
                        trades.append({
                            'code': code, 'action': '买入(部分)', 'shares': s,
                            'price': round(p, 3), 'amount': round(amount, 2),
                            'fee': round(f, 2),
                        })

            v_after = value_at(d)
            fees_paid += total_fee
            rebalances.append({
                'date': d.strftime('%Y-%m-%d'),
                'value_before': round(v_before, 2),
                'value_after': round(v_after, 2),
                'fee': round(total_fee, 2),
                'trades': trades,
                'weights_before': [
                    {'code': code, 'pct': round(shares_before[code] * float(aligned[code].loc[d]) / v_before * 100, 1)}
                    for code, _ in weights
                ],
                'weights_after': [
                    {'code': code, 'pct': round(shares[code] * float(aligned[code].loc[d]) / v_after * 100, 1)}
                    for code, _ in weights
                ],
            })

        equity.append((d, value_at(d)))

    eq = pd.Series([v for _, v in equity], index=[d for d, _ in equity])
    return {
        'equity': eq,
        'shares': shares,
        'cash': cash,
        'rebalances': rebalances,
        'fees_paid': fees_paid,
        'aligned': aligned,
    }
