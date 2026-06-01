import SwiftUI

struct DashboardView: View {
    @StateObject private var vm = DashboardViewModel()
    @State private var showTradeSheet = false

    var body: some View {
        NavigationStack {
            Group {
                if vm.isLoading && vm.account == nil {
                    LoadingView()
                } else if let error = vm.errorMessage, vm.account == nil {
                    ErrorView(message: error) { await vm.load() }
                } else {
                    ScrollView {
                        VStack(spacing: 0) {
                            // ── Hero + Chart unified block ────────────────
                            if let account = vm.account {
                                HeroChartBlock(
                                    account: account,
                                    points: vm.portfolioHistory,
                                    onPeriodChange: { period in
                                        Task { await vm.loadPortfolioHistory(period: period) }
                                    }
                                )
                            }

                            // ── Quick stats strip ─────────────────────────
                            if let account = vm.account {
                                QuickStatsStrip(account: account)
                                    .padding(.top, 14)
                                    .padding(.horizontal, KovaTheme.pagePad)
                            }

                            // ── Positions ─────────────────────────────────
                            PositionsSection(positions: vm.positions)
                                .padding(.top, 20)
                        }
                        .padding(.bottom, 24)
                    }
                    .refreshable { await vm.load() }
                    .background(KovaTheme.pageBackground)
                }
            }
            .navigationTitle("Dashboard")
            .navigationBarTitleDisplayMode(.large)
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showTradeSheet = true } label: {
                        ZStack {
                            Circle()
                                .fill(KovaTheme.purple.opacity(0.12))
                                .frame(width: 34, height: 34)
                            Image(systemName: "arrow.left.arrow.right")
                                .font(.system(size: 13, weight: .semibold))
                                .foregroundStyle(KovaTheme.purple)
                        }
                    }
                }
            }
            .sheet(isPresented: $showTradeSheet) { TradeSheet() }
        }
        .tint(KovaTheme.purple)
        .task { await vm.load() }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Hero + Chart unified block
// ─────────────────────────────────────────────────────────────────────────────

private struct HeroChartBlock: View {
    let account: AccountInfo
    let points: [PortfolioPoint]
    var onPeriodChange: ((String) -> Void)?
    @State private var selectedPeriod = "1W"
    let periods = ["1D", "1W", "1M", "3M"]
    @Environment(\.colorScheme) private var scheme

    private var isPositive: Bool {
        (points.last?.equity ?? 0) >= (points.first?.equity ?? 0)
    }

    var body: some View {
        VStack(spacing: 0) {
            // ── Portfolio value + P&L ──────────────────────────────────
            VStack(spacing: 6) {
                Text(formatCurrency(account.portfolioValue))
                    .font(.system(size: 44, weight: .bold, design: .rounded))
                    .tracking(-1.5)

                PLBadge(value: account.dayPl, percentValue: account.dayPlPercent)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 8)
            .padding(.bottom, 18)

            // ── Inline chart ───────────────────────────────────────────
            InlineChart(points: points, isPositive: isPositive)
                .frame(height: 180)
                .padding(.horizontal, 4)

            // ── Period picker ──────────────────────────────────────────
            HStack(spacing: 4) {
                ForEach(periods, id: \.self) { p in
                    Button {
                        selectedPeriod = p
                        onPeriodChange?(p)
                    } label: {
                        Text(p)
                            .font(.caption.weight(.semibold))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                            .foregroundStyle(selectedPeriod == p ? .white : .secondary)
                            .background {
                                if selectedPeriod == p {
                                    RoundedRectangle(cornerRadius: 7)
                                        .fill(KovaTheme.blueGradient)
                                } else {
                                    Color.clear
                                }
                            }
                    }
                }
            }
            .padding(4)
            .background(Color(.tertiarySystemBackground).opacity(0.7))
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .padding(.horizontal, KovaTheme.pagePad)
            .padding(.top, 10)
            .padding(.bottom, 16)
        }
    }

    private func formatCurrency(_ v: Double) -> String {
        let f = NumberFormatter(); f.numberStyle = .currency; f.maximumFractionDigits = 0
        return f.string(from: NSNumber(value: v)) ?? "$\(Int(v))"
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Inline Chart (no card wrapper)
// ─────────────────────────────────────────────────────────────────────────────

import Charts

private struct InlineChart: View {
    let points: [PortfolioPoint]
    let isPositive: Bool
    @Environment(\.colorScheme) private var scheme

    private var lineColor: Color { isPositive ? KovaTheme.positive : KovaTheme.negative }

    var body: some View {
        if points.isEmpty {
            Text("No history yet")
                .foregroundStyle(.secondary)
                .font(.subheadline)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Chart(points) { pt in
                LineMark(x: .value("Date", pt.date), y: .value("Equity", pt.equity))
                    .foregroundStyle(lineColor)
                    .lineStyle(StrokeStyle(lineWidth: 2.5))
                    .interpolationMethod(.catmullRom)

                AreaMark(x: .value("Date", pt.date), y: .value("Equity", pt.equity))
                    .foregroundStyle(LinearGradient(
                        colors: [lineColor.opacity(scheme == .dark ? 0.30 : 0.20), .clear],
                        startPoint: .top, endPoint: .bottom))
                    .interpolationMethod(.catmullRom)
            }
            .chartXAxis(.hidden)
            .chartYAxis {
                AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { v in
                    AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5, dash: [4]))
                        .foregroundStyle(Color(.separator).opacity(0.4))
                    AxisValueLabel {
                        if let val = v.as(Double.self) {
                            Text("$\(Int(val/1000))k").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Quick stats strip
// ─────────────────────────────────────────────────────────────────────────────

private struct QuickStatsStrip: View {
    let account: AccountInfo

    var body: some View {
        HStack(spacing: 10) {
            DashStatPill(label: "Tradeable Cash",
                     value: formatK(account.tradeableCash ?? account.cash),
                     icon: "banknote",
                     color: KovaTheme.positive)
            DashStatPill(label: "Buying Power",
                     value: formatK(account.buyingPower),
                     icon: "bolt.fill",
                     color: KovaTheme.purple)
        }
    }

    private func formatK(_ v: Double) -> String {
        v >= 1000 ? String(format: "$%.1fk", v/1000) : String(format: "$%.0f", v)
    }
}

private struct DashStatPill: View {
    let label: String
    let value: String
    let icon: String
    let color: Color

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 13))
                .foregroundStyle(color)
                .frame(width: 28, height: 28)
                .background(color.opacity(0.12))
                .clipShape(Circle())
            VStack(alignment: .leading, spacing: 1) {
                Text(label).font(.caption).foregroundStyle(.secondary)
                Text(value).font(.subheadline.weight(.semibold))
            }
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .kovaCard(padded: false)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Positions section
// ─────────────────────────────────────────────────────────────────────────────

private struct PositionsSection: View {
    let positions: [Position]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Positions")
                    .font(.title3.weight(.bold))
                if !positions.isEmpty {
                    Text("\(positions.count)")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.white)
                        .frame(width: 20, height: 20)
                        .background(KovaTheme.purple)
                        .clipShape(Circle())
                }
                Spacer()
            }
            .padding(.horizontal, KovaTheme.pagePad)

            if positions.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "chart.pie")
                        .font(.system(size: 36))
                        .foregroundStyle(KovaTheme.purple.opacity(0.4))
                    Text("No open positions")
                        .foregroundStyle(.secondary)
                        .font(.subheadline)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 32)
                .kovaCard()
                .padding(.horizontal, KovaTheme.pagePad)
            } else {
                VStack(spacing: 8) {
                    ForEach(positions) { pos in
                        PositionRowView(position: pos)
                    }
                }
                .padding(.horizontal, KovaTheme.pagePad)
            }
        }
    }
}
