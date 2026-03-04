import React, { useState, useMemo } from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';

const ProfitRolloverSimulator = () => {
  const [winRate, setWinRate] = useState(50);
  const [surrenderRate, setSurrenderRate] = useState(30);
  const [rr55Min, setRr55Min] = useState(1.75);
  const [rr55Max, setRr55Max] = useState(2.5);
  const [rr30Min, setRr30Min] = useState(2.5);
  const [rr30Max, setRr30Max] = useState(4.0);
  const [rr15Min, setRr15Min] = useState(4.0);
  const [rr15Max, setRr15Max] = useState(6.0);
  const [minPrice, setMinPrice] = useState(2000);
  const [maxPrice, setMaxPrice] = useState(3000);
  const [startingCapital, setStartingCapital] = useState(400);
  const [baseRisk, setBaseRisk] = useState(4.95);
  const [baseRiskSizeETH, setBaseRiskSizeETH] = useState(0.33);
  const [numTrades, setNumTrades] = useState(100);
  const [iterations, setIterations] = useState(1000);
  const [selectedSimulation, setSelectedSimulation] = useState(0);

  const lossSequence = [1, 1, 2, 3, 5, 8];

  const runSimulation = () => {
    const winRateDecimal = winRate / 100;
    const surrenderRateDecimal = surrenderRate / 100;

    const getRandomRR = () => {
      const rand = Math.random();
      if (rand < 0.55) {
        const range = rr55Max - rr55Min;
        const splitPoint = rr55Min + (range * 0.25);
        if (Math.random() < 0.7) {
          return splitPoint + Math.random() * (rr55Max - splitPoint);
        } else {
          return rr55Min + Math.random() * (splitPoint - rr55Min);
        }
      } else if (rand < 0.85) {
        return rr30Min + Math.random() * (rr30Max - rr30Min);
      } else {
        return rr15Min + Math.random() * (rr15Max - rr15Min);
      }
    };

    const calculateFees = (totalRiskMultiplier) => {
      const tradePrice = minPrice + Math.random() * (maxPrice - minPrice);
      const scaledSizeETH = baseRiskSizeETH * totalRiskMultiplier;
      return 2 * (0.0006 * scaledSizeETH * tradePrice);
    };

    const allSimulations = [];
    const allTrades = [];
    let globalMaxWinStreak = 0;
    let globalMaxLossStreak = 0;
    let totalRRSum = 0;
    let totalRRCount = 0;
    let totalMaxDrawdown = 0;

    for (let sim = 0; sim < iterations; sim++) {
      let capital = startingCapital;
      const equityCurve = [{ trade: 0, equity: capital, simulation: sim }];
      const simTrades = [];

      let lossSequenceIndex = 0;
      let consecutiveLosses = 0;
      let consecutiveSurrenders = 0;
      let profitRollover = 0;
      let isRolloverActive = false;
      let currentBaseRisk = baseRisk;
      let returnThreshold = startingCapital * 2;
      let currentWinStreak = 0;
      let currentLossStreak = 0;
      let maxWinStreak = 0;
      let maxLossStreak = 0;
      let peakEquity = startingCapital;
      let maxDrawdown = 0;

      for (let trade = 1; trade <= numTrades; trade++) {
        if (capital <= 0) {
          simTrades.push({
            trade, result: 'Total Loss', riskMultiplier: 0, currentBaseRisk: 0,
            risk: 0, rollover: 0, totalRisk: 0, positionSize: 0, rr: 0,
            grossPnL: 0, fees: 0, netPnL: 0, equity: 0,
            consecutiveLosses: 0, consecutiveSurrenders: 0, progressionType: 'None'
          });
          equityCurve.push({ trade, equity: 0, simulation: sim });
          continue;
        }

        while (capital >= returnThreshold) {
          currentBaseRisk = currentBaseRisk * 2;
          returnThreshold = returnThreshold * 2;
        }

        const baseRiskMultiplier = lossSequence[lossSequenceIndex];
        const sequenceRisk = currentBaseRisk * baseRiskMultiplier;
        const rolloverAmount = profitRollover;
        const totalRiskAmountRaw = sequenceRisk + rolloverAmount;
        const totalRiskAmount = Math.floor(totalRiskAmountRaw);
        const totalRiskMultiplier = totalRiskAmount / baseRisk;
        const positionSizeETH = baseRiskSizeETH * totalRiskMultiplier;
        const progressionType = rolloverAmount > 0 ? 'Profit Rollover' : 'Loss Recovery';

        const isSurrender = Math.random() < surrenderRateDecimal;
        let isWin = false;
        let rr = 0;
        let grossPnL = 0;
        let feeAmount = 0;
        let netPnL = 0;

        if (isSurrender) {
          feeAmount = calculateFees(totalRiskMultiplier);
          const fractionalMultiplier = Math.random() * 0.3;
          const fractionalPnL = fractionalMultiplier * totalRiskAmount;
          const isProfitSurrender = Math.random() < 0.5;
          if (isProfitSurrender) {
            grossPnL = fractionalPnL;
            netPnL = grossPnL - feeAmount;
            isWin = netPnL > 0;
          } else {
            grossPnL = -fractionalPnL;
            netPnL = grossPnL - feeAmount;
            isWin = false;
          }
          rr = 0;
        } else {
          isWin = Math.random() < winRateDecimal;
          rr = getRandomRR();
          if (isWin) {
            grossPnL = totalRiskAmount * rr;
            feeAmount = calculateFees(totalRiskMultiplier);
            netPnL = grossPnL - feeAmount;
            totalRRSum += rr;
            totalRRCount++;
          } else {
            grossPnL = -totalRiskAmount;
            feeAmount = calculateFees(totalRiskMultiplier);
            netPnL = grossPnL - feeAmount;
          }
        }

        capital += netPnL;
        if (capital < 0) capital = 0;

        if (capital > peakEquity) peakEquity = capital;
        const drawdown = peakEquity > 0 ? ((peakEquity - capital) / peakEquity) * 100 : 0;
        if (drawdown > maxDrawdown) maxDrawdown = drawdown;

        if (isSurrender) {
          consecutiveSurrenders++;
          if (consecutiveSurrenders >= 3) {
            consecutiveLosses++;
            consecutiveSurrenders = 0;
            profitRollover = 0;
            isRolloverActive = false;
            if (consecutiveLosses >= 5) {
              lossSequenceIndex = 0;
              consecutiveLosses = 0;
            } else {
              if (lossSequenceIndex < lossSequence.length - 1) lossSequenceIndex++;
            }
          }
        } else if (isWin) {
          consecutiveLosses = 0;
          consecutiveSurrenders = 0;
          currentLossStreak = 0;
          currentWinStreak++;
          if (currentWinStreak > maxWinStreak) maxWinStreak = currentWinStreak;
          if (lossSequenceIndex > 0) lossSequenceIndex--;
          if (isRolloverActive) {
            profitRollover = 0;
            isRolloverActive = false;
          } else {
            profitRollover = netPnL;
            isRolloverActive = true;
          }
        } else {
          consecutiveLosses++;
          consecutiveSurrenders = 0;
          profitRollover = 0;
          isRolloverActive = false;
          currentWinStreak = 0;
          currentLossStreak++;
          if (currentLossStreak > maxLossStreak) maxLossStreak = currentLossStreak;
          if (consecutiveLosses >= 5) {
            lossSequenceIndex = 0;
            consecutiveLosses = 0;
          } else {
            if (lossSequenceIndex < lossSequence.length - 1) lossSequenceIndex++;
          }
        }

        equityCurve.push({ trade, equity: capital, simulation: sim });
        simTrades.push({
          trade,
          result: isSurrender ? (isWin ? 'Surrender (Profit)' : 'Surrender (Loss)') : (isWin ? 'Win' : 'Loss'),
          riskMultiplier: baseRiskMultiplier,
          currentBaseRisk,
          risk: sequenceRisk,
          rollover: rolloverAmount,
          isRolloverTrade: rolloverAmount > 0,
          totalRisk: totalRiskAmount,
          positionSize: positionSizeETH,
          rr,
          grossPnL,
          fees: feeAmount,
          netPnL,
          equity: capital,
          consecutiveLosses: isWin ? 0 : consecutiveLosses,
          consecutiveSurrenders,
          progressionType
        });
      }

      allSimulations.push(equityCurve);
      allTrades.push(simTrades);
      totalMaxDrawdown += maxDrawdown;

      if (maxWinStreak > globalMaxWinStreak) globalMaxWinStreak = maxWinStreak;
      if (maxLossStreak > globalMaxLossStreak) globalMaxLossStreak = maxLossStreak;
    }

    const avgRR = totalRRCount > 0 ? totalRRSum / totalRRCount : 0;
    const avgMaxDrawdown = totalMaxDrawdown / iterations;

    return {
      simulations: allSimulations,
      trades: allTrades,
      maxWinStreak: globalMaxWinStreak,
      maxLossStreak: globalMaxLossStreak,
      avgRR,
      avgMaxDrawdown
    };
  };

  const result = useMemo(() => runSimulation(), [
    startingCapital, baseRisk, baseRiskSizeETH, minPrice, maxPrice, iterations, numTrades,
    winRate, surrenderRate, rr55Min, rr55Max, rr30Min, rr30Max, rr15Min, rr15Max
  ]);

  const { simulations, trades, maxWinStreak, maxLossStreak, avgRR, avgMaxDrawdown } = result;

  const statistics = useMemo(() => {
    if (!simulations || simulations.length === 0) {
      return {
        avgFinal: '0.00', maxFinal: '0.00', minFinal: '0.00', medianFinal: '0.00',
        avgReturn: '0.00', maxReturn: '0.00', minReturn: '0.00',
        probProfit: '0.0', probBust: '0.0', probTotalLoss: '0.0',
        prob5x: '0.0', prob10x: '0.0', prob15x: '0.0', prob20x: '0.0', prob25x: '0.0',
        maxWinStreak: 0, maxLossStreak: 0,
        avgRR: '0.00', breakEvenRR: '0.00', avgMaxDrawdown: '0.00'
      };
    }

    const finalEquities = simulations.map(sim => sim[sim.length - 1].equity);
    const finalReturns = finalEquities.map(eq => ((eq - startingCapital) / startingCapital) * 100);

    const avgFinal = finalEquities.reduce((a, b) => a + b, 0) / finalEquities.length;
    const maxFinal = Math.max(...finalEquities);
    const minFinal = Math.min(...finalEquities);

    const avgReturn = finalReturns.reduce((a, b) => a + b, 0) / finalReturns.length;
    const maxReturn = Math.max(...finalReturns);
    const minReturn = Math.min(...finalReturns);

    const profitable = finalEquities.filter(eq => eq > startingCapital).length;
    const probProfit = (profitable / iterations) * 100;
    const totalLoss = finalEquities.filter(eq => eq === 0).length;
    const probTotalLoss = (totalLoss / iterations) * 100;

    const hit5x = finalEquities.filter(eq => eq >= startingCapital * 5).length;
    const prob5x = (hit5x / iterations) * 100;
    const hit10x = finalEquities.filter(eq => eq >= startingCapital * 10).length;
    const prob10x = (hit10x / iterations) * 100;
    const hit15x = finalEquities.filter(eq => eq >= startingCapital * 15).length;
    const prob15x = (hit15x / iterations) * 100;
    const hit20x = finalEquities.filter(eq => eq >= startingCapital * 20).length;
    const prob20x = (hit20x / iterations) * 100;
    const hit25x = finalEquities.filter(eq => eq >= startingCapital * 25).length;
    const prob25x = (hit25x / iterations) * 100;

    // Break-even Win Rate: minimum win rate to break even given the average R:R
    // E[PnL] = winRate * avgRR - (1 - winRate) = 0  =>  winRate = 1 / (1 + avgRR)
    const breakEvenWinRate = avgRR > 0 ? (1 / (1 + avgRR)) * 100 : 0;

    return {
      avgFinal: avgFinal.toFixed(2),
      maxFinal: maxFinal.toFixed(2),
      minFinal: minFinal.toFixed(2),
      avgReturn: avgReturn.toFixed(2),
      maxReturn: maxReturn.toFixed(2),
      minReturn: minReturn.toFixed(2),
      probProfit: probProfit.toFixed(1),
      probTotalLoss: probTotalLoss.toFixed(1),
      prob5x: prob5x.toFixed(1), prob10x: prob10x.toFixed(1),
      prob15x: prob15x.toFixed(1), prob20x: prob20x.toFixed(1), prob25x: prob25x.toFixed(1),
      maxWinStreak, maxLossStreak,
      avgRR: avgRR.toFixed(2),
      breakEvenWinRate: breakEvenWinRate.toFixed(1),
      avgMaxDrawdown: avgMaxDrawdown.toFixed(2)
    };
  }, [simulations, startingCapital, iterations, maxWinStreak, maxLossStreak, avgRR, avgMaxDrawdown, winRate]);

  const percentileData = useMemo(() => {
    if (!simulations || simulations.length === 0) return [];
    const data = [];
    for (let i = 0; i <= numTrades; i++) {
      const equitiesAtTrade = simulations.map(sim => sim[i]?.equity || 0).sort((a, b) => a - b);
      const p10 = equitiesAtTrade[Math.floor(equitiesAtTrade.length * 0.1)] || 0;
      const p50 = equitiesAtTrade[Math.floor(equitiesAtTrade.length * 0.5)] || 0;
      const p90 = equitiesAtTrade[Math.floor(equitiesAtTrade.length * 0.9)] || 0;
      data.push({ trade: i, p10, p50, p90 });
    }
    return data;
  }, [simulations, numTrades]);

  const avgRRValue = parseFloat(statistics.avgRR);
  const breakEvenWinRateValue = parseFloat(statistics.breakEvenWinRate);
  const winRateEdge = winRate - breakEvenWinRateValue;
  const winRateEdgePositive = winRateEdge >= 0;

  return (
    <div className="w-full max-w-7xl mx-auto p-6 bg-gray-50">
      <h1 className="text-3xl font-bold mb-6 text-gray-800">Profit Rollover Risk Simulator</h1>

      {/* Rules: Loss Progression + Win Progression + Surrender */}
      <div className="bg-white rounded-lg shadow-md p-6 mb-6">
        <h2 className="text-xl font-semibold mb-4 text-gray-700">Rules</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="bg-red-50 p-4 rounded">
            <p className="font-semibold text-red-900 mb-2">Loss Progression</p>
            <p className="text-sm text-red-800 mb-2">1R → 1R → 2R → 3R → 5R → 8R (CAP)</p>
            <ul className="text-xs text-red-700 space-y-1">
              <li>• After loss: move forward</li>
              <li>• After win: move back</li>
              <li>• <strong>5 consecutive losses: RESET to 1R</strong></li>
              <li>• <strong className="text-orange-700">Every 100% return: Base risk (1R) DOUBLES</strong></li>
            </ul>
          </div>

          <div className="bg-green-50 p-4 rounded">
            <p className="font-semibold text-green-900 mb-2">Win Progression (1-2 Combo)</p>
            <ul className="text-xs text-green-700 space-y-1">
              <li>• <strong>1st win</strong>: Profit rolls into NEXT trade</li>
              <li>• <strong>Next trade (2nd)</strong>: Risk = sequence + rollover</li>
              <li>• <strong>If 2nd wins</strong>: Rollover CLEARED → normal</li>
              <li>• <strong>If 2nd loses</strong>: Rollover CLEARED → Loss Recovery</li>
            </ul>
          </div>

          <div className="bg-yellow-50 p-4 rounded">
            <p className="font-semibold text-yellow-900 mb-2">Surrender Trades</p>
            <ul className="text-xs text-yellow-700 space-y-1">
              <li>• Exit at 0–30% of risk (random)</li>
              <li>• 50% profit / 50% loss (random)</li>
              <li>• Fees always applied</li>
              <li>• <strong>3 consecutive surrenders = 1 loss</strong></li>
            </ul>
          </div>
        </div>
      </div>

      {/* Parameters */}
      <div className="bg-white rounded-lg shadow-md p-6 mb-6">
        <h2 className="text-xl font-semibold mb-4 text-gray-700">Parameters</h2>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Win Rate (%)</label>
            <input type="number" step="1" min="0" max="100" value={winRate}
              onChange={(e) => setWinRate(Number(e.target.value))}
              className="w-full px-4 py-2 border border-gray-300 rounded-md" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Surrender Trades (%)</label>
            <input type="number" step="1" min="0" max="100" value={surrenderRate}
              onChange={(e) => setSurrenderRate(Number(e.target.value))}
              className="w-full px-4 py-2 border border-gray-300 rounded-md" />
            <p className="text-xs text-gray-500 mt-1">% of trades exited early</p>
          </div>
        </div>

        <div className="bg-blue-50 p-4 rounded mb-6">
          <p className="text-sm font-semibold text-blue-900 mb-3">Variable R:R Distribution</p>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <p className="text-xs font-semibold text-blue-800 mb-2">55% of trades</p>
              <div className="flex gap-2 items-center">
                <input type="number" step="0.1" value={rr55Min}
                  onChange={(e) => setRr55Min(Number(e.target.value))}
                  className="w-20 px-2 py-1 border rounded text-sm" />
                <span className="text-blue-700">to</span>
                <input type="number" step="0.1" value={rr55Max}
                  onChange={(e) => setRr55Max(Number(e.target.value))}
                  className="w-20 px-2 py-1 border rounded text-sm" />
              </div>
            </div>
            <div>
              <p className="text-xs font-semibold text-blue-800 mb-2">30% of trades</p>
              <div className="flex gap-2 items-center">
                <input type="number" step="0.1" value={rr30Min}
                  onChange={(e) => setRr30Min(Number(e.target.value))}
                  className="w-20 px-2 py-1 border rounded text-sm" />
                <span className="text-blue-700">to</span>
                <input type="number" step="0.1" value={rr30Max}
                  onChange={(e) => setRr30Max(Number(e.target.value))}
                  className="w-20 px-2 py-1 border rounded text-sm" />
              </div>
            </div>
            <div>
              <p className="text-xs font-semibold text-blue-800 mb-2">15% of trades</p>
              <div className="flex gap-2 items-center">
                <input type="number" step="0.1" value={rr15Min}
                  onChange={(e) => setRr15Min(Number(e.target.value))}
                  className="w-20 px-2 py-1 border rounded text-sm" />
                <span className="text-blue-700">to</span>
                <input type="number" step="0.1" value={rr15Max}
                  onChange={(e) => setRr15Max(Number(e.target.value))}
                  className="w-20 px-2 py-1 border rounded text-sm" />
              </div>
            </div>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Min ETH Price ($)</label>
            <input type="number" step="1" min="0" value={minPrice}
              onChange={(e) => setMinPrice(Number(e.target.value))}
              className="w-full px-4 py-2 border border-gray-300 rounded-md" />
            <p className="text-xs text-gray-500 mt-1">Lower bound for random price</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Max ETH Price ($)</label>
            <input type="number" step="1" min="0" value={maxPrice}
              onChange={(e) => setMaxPrice(Number(e.target.value))}
              className="w-full px-4 py-2 border border-gray-300 rounded-md" />
            <p className="text-xs text-gray-500 mt-1">Upper bound for random price</p>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-6">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Starting Capital ($)</label>
            <input type="number" step="1" min="0" value={startingCapital}
              onChange={(e) => setStartingCapital(Number(e.target.value))}
              className="w-full px-4 py-2 border border-gray-300 rounded-md" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Base Risk (1R) ($)</label>
            <input type="number" step="0.01" min="0" value={baseRisk}
              onChange={(e) => setBaseRisk(Number(e.target.value))}
              className="w-full px-4 py-2 border border-gray-300 rounded-md" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">1R Size (ETH)</label>
            <input type="number" step="0.001" min="0" value={baseRiskSizeETH}
              onChange={(e) => setBaseRiskSizeETH(Number(e.target.value))}
              className="w-full px-4 py-2 border border-gray-300 rounded-md" />
            <p className="text-xs text-gray-500 mt-1">Fee = 2×(0.0006×Size×Price)</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Trades</label>
            <input type="number" min="1" value={numTrades}
              onChange={(e) => setNumTrades(Number(e.target.value))}
              className="w-full px-4 py-2 border border-gray-300 rounded-md" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Simulations</label>
            <input type="number" min="1" max="5000" value={iterations}
              onChange={(e) => setIterations(Number(e.target.value))}
              className="w-full px-4 py-2 border border-gray-300 rounded-md" />
          </div>
        </div>
      </div>

      {/* Multiplication Probabilities */}
      <div className="bg-white rounded-lg shadow-md p-6 mb-6">
        <h2 className="text-xl font-semibold mb-4 text-gray-700">Multiplication Probabilities</h2>
        <div className="grid grid-cols-5 gap-4">
          <div className="bg-blue-50 p-4 rounded border-2 border-blue-300">
            <p className="text-sm text-gray-600">5x</p>
            <p className="text-2xl font-bold text-blue-600">{statistics.prob5x}%</p>
          </div>
          <div className="bg-indigo-50 p-4 rounded border-2 border-indigo-300">
            <p className="text-sm text-gray-600">10x</p>
            <p className="text-2xl font-bold text-indigo-600">{statistics.prob10x}%</p>
          </div>
          <div className="bg-purple-50 p-4 rounded border-2 border-purple-300">
            <p className="text-sm text-gray-600">15x</p>
            <p className="text-2xl font-bold text-purple-600">{statistics.prob15x}%</p>
          </div>
          <div className="bg-pink-50 p-4 rounded border-2 border-pink-300">
            <p className="text-sm text-gray-600">20x</p>
            <p className="text-2xl font-bold text-pink-600">{statistics.prob20x}%</p>
          </div>
          <div className="bg-rose-50 p-4 rounded border-2 border-rose-300">
            <p className="text-sm text-gray-600">25x</p>
            <p className="text-2xl font-bold text-rose-600">{statistics.prob25x}%</p>
          </div>
        </div>
      </div>

      {/* Statistics */}
      <div className="bg-white rounded-lg shadow-md p-6 mb-6">
        <h2 className="text-xl font-semibold mb-4 text-gray-700">Statistics</h2>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
          <div className="bg-green-50 p-4 rounded">
            <p className="text-sm text-gray-600">Avg Final</p>
            <p className="text-2xl font-bold text-green-600">${statistics.avgFinal}</p>
            <p className="text-xs text-gray-500">{statistics.avgReturn}%</p>
          </div>
          <div className="bg-emerald-50 p-4 rounded">
            <p className="text-sm text-gray-600">Best</p>
            <p className="text-2xl font-bold text-emerald-600">${statistics.maxFinal}</p>
            <p className="text-xs text-gray-500">{statistics.maxReturn}%</p>
          </div>
          <div className="bg-red-50 p-4 rounded">
            <p className="text-sm text-gray-600">Worst</p>
            <p className="text-2xl font-bold text-red-600">${statistics.minFinal}</p>
            <p className="text-xs text-gray-500">{statistics.minReturn}%</p>
          </div>
          <div className="bg-purple-50 p-4 rounded">
            <p className="text-sm text-gray-600">Profitable</p>
            <p className="text-2xl font-bold text-purple-600">{statistics.probProfit}%</p>
          </div>
          <div className="bg-red-50 p-4 rounded border-2 border-red-300">
            <p className="text-sm text-gray-600">Busted</p>
            <p className="text-2xl font-bold text-red-700">{statistics.probTotalLoss}%</p>
          </div>
          <div className="bg-green-50 p-4 rounded border-2 border-green-300">
            <p className="text-sm text-gray-600">Max Win Streak</p>
            <p className="text-2xl font-bold text-green-700">{statistics.maxWinStreak}</p>
          </div>
          <div className="bg-red-50 p-4 rounded border-2 border-red-400">
            <p className="text-sm text-gray-600">Max Loss Streak</p>
            <p className="text-2xl font-bold text-red-800">{statistics.maxLossStreak}</p>
          </div>
          <div className="bg-orange-50 p-4 rounded border-2 border-orange-300">
            <p className="text-sm text-gray-600">Avg Max Drawdown</p>
            <p className="text-2xl font-bold text-orange-700">{statistics.avgMaxDrawdown}%</p>
          </div>
        </div>

        {/* R:R Stats */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="bg-sky-50 p-4 rounded border-2 border-sky-300">
            <p className="text-sm text-gray-600">Average R:R</p>
            <p className="text-2xl font-bold text-sky-600">1:{statistics.avgRR}</p>
            <p className="text-xs text-gray-500">Across all winning trades</p>
          </div>
          <div className="bg-slate-50 p-4 rounded border-2 border-slate-300">
            <p className="text-sm text-gray-600">Break-Even Win Rate</p>
            <p className="text-2xl font-bold text-slate-600">{statistics.breakEvenWinRate}%</p>
            <p className="text-xs text-gray-500">Min win rate to break even at 1:{statistics.avgRR} R:R</p>
          </div>
          <div className={`p-4 rounded border-2 ${winRateEdgePositive ? 'bg-green-50 border-green-400' : 'bg-red-50 border-red-400'}`}>
            <p className="text-sm text-gray-600">Win Rate Edge</p>
            <p className={`text-2xl font-bold ${winRateEdgePositive ? 'text-green-700' : 'text-red-700'}`}>
              {winRateEdgePositive ? '+' : ''}{winRateEdge.toFixed(1)}%
            </p>
            <p className="text-xs text-gray-500">{winRateEdgePositive ? 'Positive edge' : 'Negative edge'} vs break-even</p>
          </div>
        </div>
      </div>

      {/* Equity Curves */}
      <div className="bg-white rounded-lg shadow-md p-6 mb-6">
        <h2 className="text-xl font-semibold mb-4 text-gray-700">Equity Curves</h2>
        <ResponsiveContainer width="100%" height={400}>
          <LineChart data={percentileData}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="trade" />
            <YAxis tickFormatter={(v) => `$${(v / 1000).toFixed(1)}k`} />
            <Tooltip formatter={(v) => `$${v.toFixed(2)}`} />
            <Legend />
            <Line type="monotone" dataKey="p90" stroke="#10b981" name="90th %" strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="p50" stroke="#3b82f6" name="Median" strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="p10" stroke="#ef4444" name="10th %" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Trade Breakdown */}
      <div className="bg-white rounded-lg shadow-md p-6">
        <h2 className="text-xl font-semibold mb-4 text-gray-700">Trade Breakdown</h2>
        <select value={selectedSimulation} onChange={(e) => setSelectedSimulation(Number(e.target.value))}
          className="w-64 px-4 py-2 border mb-4 rounded-md">
          {Array.from({ length: Math.min(iterations, 100) }, (_, i) => (
            <option key={i} value={i}>Simulation {i + 1}</option>
          ))}
        </select>

        <div className="overflow-x-auto overflow-y-auto max-h-96">
          <table className="divide-y divide-gray-200 text-xs" style={{minWidth: '900px', width: '100%'}}>
            <thead className="bg-gray-50 sticky top-0">
              <tr>
                <th style={{width:'36px'}}  className="px-1 py-2 text-center font-medium text-gray-500">#</th>
                <th style={{width:'110px'}} className="px-1 py-2 text-center font-medium text-gray-500">Result</th>
                <th style={{width:'100px'}} className="px-1 py-2 text-center font-medium text-gray-500">Type</th>
                <th style={{width:'60px'}}  className="px-1 py-2 text-center font-medium text-gray-500">Seq Risk</th>
                <th style={{width:'58px'}}  className="px-1 py-2 text-right font-medium text-gray-500">1R</th>
                <th style={{width:'68px'}}  className="px-1 py-2 text-right font-medium text-gray-500">Rollover</th>
                <th style={{width:'68px'}}  className="px-1 py-2 text-right font-medium text-gray-500">Tot. Risk</th>
                <th style={{width:'72px'}}  className="px-1 py-2 text-right font-medium text-gray-500">Size (ETH)</th>
                <th style={{width:'58px'}}  className="px-1 py-2 text-center font-medium text-gray-500">R:R</th>
                <th style={{width:'76px'}}  className="px-1 py-2 text-right font-medium text-gray-500">Gross P&L</th>
                <th style={{width:'60px'}}  className="px-1 py-2 text-right font-medium text-gray-500">Fees</th>
                <th style={{width:'72px'}}  className="px-1 py-2 text-right font-medium text-gray-500">Net P&L</th>
                <th style={{width:'72px'}}  className="px-1 py-2 text-right font-medium text-gray-500">Equity</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {trades[selectedSimulation]?.map((t) => (
                <tr key={t.trade} className={t.result === 'Total Loss' ? 'bg-red-50' : 'hover:bg-gray-50'}>
                  <td className="px-1 py-1.5 text-center text-gray-500">{t.trade}</td>
                  <td className="px-1 py-1.5 text-center">
                    <span className={`px-1.5 py-0.5 rounded font-medium whitespace-nowrap ${
                      t.result === 'Win' ? 'bg-green-100 text-green-800' :
                      t.result === 'Surrender (Profit)' ? 'bg-yellow-100 text-yellow-700' :
                      t.result === 'Surrender (Loss)' ? 'bg-orange-100 text-orange-700' :
                      t.result === 'Total Loss' ? 'bg-gray-300 text-gray-700' :
                      'bg-red-100 text-red-800'
                    }`}>{t.result}</span>
                  </td>
                  <td className="px-1 py-1.5 text-center">
                    {t.result !== 'Total Loss' && (
                      <span className={`px-1.5 py-0.5 rounded whitespace-nowrap ${
                        t.progressionType === 'Profit Rollover' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'
                      }`}>{t.progressionType}</span>
                    )}
                  </td>
                  <td className="px-1 py-1.5 text-center">
                    {t.result !== 'Total Loss' && (
                      <span className="font-bold text-blue-600">
                        {t.isRolloverTrade
                          ? <span>{t.riskMultiplier}R<span className="text-green-600">+W</span></span>
                          : `${t.riskMultiplier}R`}
                        {t.consecutiveLosses === 5 && <span className="ml-1 text-orange-600">↺</span>}
                        {t.consecutiveSurrenders === 3 && <span className="ml-1 text-yellow-600">S3</span>}
                      </span>
                    )}
                  </td>
                  <td className="px-1 py-1.5 text-right font-medium text-orange-600">
                    {t.currentBaseRisk > 0 ? `$${t.currentBaseRisk.toFixed(2)}` : '-'}
                  </td>
                  <td className="px-1 py-1.5 text-right text-green-600 font-medium">
                    {t.rollover > 0 ? `+${t.rollover.toFixed(2)}` : '-'}
                  </td>
                  <td className="px-1 py-1.5 text-right font-bold text-purple-600">
                    ${t.totalRisk.toFixed(2)}
                  </td>
                  <td className="px-1 py-1.5 text-right text-indigo-600 font-medium">
                    {t.positionSize > 0 ? `${t.positionSize.toFixed(3)}` : '-'}
                  </td>
                  <td className="px-1 py-1.5 text-center text-gray-700 font-medium">
                    {t.rr > 0 ? `1:${t.rr.toFixed(2)}` : (t.result.includes('Surrender') ? 'Surr' : '-')}
                  </td>
                  <td className={`px-1 py-1.5 text-right font-medium ${t.grossPnL >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                    ${t.grossPnL.toFixed(2)}
                  </td>
                  <td className="px-1 py-1.5 text-right text-red-600">
                    ${t.fees.toFixed(2)}
                  </td>
                  <td className={`px-1 py-1.5 text-right font-medium ${t.netPnL >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                    ${t.netPnL.toFixed(2)}
                  </td>
                  <td className="px-1 py-1.5 text-right font-semibold">${t.equity.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};

export default ProfitRolloverSimulator;
