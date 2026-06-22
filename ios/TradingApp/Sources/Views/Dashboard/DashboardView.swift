import SwiftUI
import Charts

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
                    mainContent
                }
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 6) {
                        Text("🪷").font(.system(size: 15))
                        Text("Portfolio").font(.headline.weight(.bold))
                    }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button { showTradeSheet = true } label: {
                        ZStack {
                            Capsule()
                                .fill(LakshmiTheme.brandGradient)
                                .frame(width: 60, height: 30)
                            Text("Trade")
                                .font(.caption.weight(.bold))
                                .foregroundStyle(.white)
                        }
                    }
                    .buttonStyle(PressScaleButtonStyle(scale: 0.93))
                }
            }
            .sheet(isPresented: $showTradeSheet) { TradeSheet() }
        }
        .tint(LakshmiTheme.gold)
        .task { await vm.load() }
    }

    @ViewBuilder
    private var mainContent: some View {
        ScrollView {
            VStack(spacing: 0) {
                // ── Rolling ticker — always visible ────────────────────────
                LiveTickerBanner(positions: vm.positions)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(
                        LakshmiTheme.brandGradient.opacity(0.08)
                            .overlay(Divider().frame(maxHeight: .infinity, alignment: .bottom))
                    )

                // ── Hero portfolio block — full-width (8pt margins) ────────
                if let account = vm.account {
                    HeroChartBlock(
                        account: account,
                        points: vm.portfolioHistory,
                        onPeriodChange: { period in Task { await vm.loadPortfolioHistory(period: period) } }
                    )
                    .padding(.horizontal, 8)
                    .padding(.top, 12)
                    .cardAppear(delay: 0.0)
                }

                // ── Cards below hero ──────────────────────────────────────
                VStack(spacing: 20) {
                    // ── Stat cards ─────────────────────────────────────────
                    if let account = vm.account {
                        HStack(spacing: 10) {
                            GlassStatCard(icon: "banknote", label: "Cash",
                                          value: formatK(account.cash), color: LakshmiTheme.positive)
                            GlassStatCard(icon: "bolt.fill", label: "Buying Power",
                                          value: formatK(account.buyingPower), color: LakshmiTheme.gold)
                            GlassStatCard(icon: "clock.arrow.2.circlepath", label: "Day P&L",
                                          value: formatPL(account.dayPl), color: account.dayPl >= 0 ? LakshmiTheme.positive : LakshmiTheme.negative)
                        }
                    }

                    // ── Positions ──────────────────────────────────────────
                    PositionsSection(positions: vm.positions)
                }
                .padding(.horizontal, 16)
                .padding(.top, 16)
                .padding(.bottom, 32)
            }
        }
        .refreshable { await vm.load() }
        .background(LakshmiTheme.pageBackground)
    }

    private func formatK(_ v: Double) -> String {
        v >= 1000 ? String(format: "$%.1fk", v / 1000) : String(format: "$%.0f", v)
    }

    private func formatPL(_ v: Double) -> String {
        String(format: "%@$%.0f", v >= 0 ? "+" : "", v)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Hero + Chart block
// ─────────────────────────────────────────────────────────────────────────────

private struct HeroChartBlock: View {
    let account: AccountInfo
    let points: [PortfolioPoint]
    var onPeriodChange: ((String) -> Void)?

    @State private var selectedPeriod = "1W"
    @State private var breathe = false
    @Namespace private var periodNS
    let periods = ["1D", "1W", "1M", "3M"]
    @Environment(\.colorScheme) private var scheme

    private var isPositive: Bool {
        (points.last?.equity ?? 0) >= (points.first?.equity ?? 0)
    }

    var body: some View {
        VStack(spacing: 0) {
            // ── Dark branded hero — value + P&L ───────────────────────────
            ZStack {
                AnimatedOrbBackground()

                VStack(spacing: 6) {
                    Text("PORTFOLIO")
                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                        .tracking(3)
                        .foregroundStyle(.white.opacity(0.40))

                    Text(formatCurrency(account.portfolioValue))
                        .font(.system(size: 48, weight: .bold, design: .rounded))
                        .tracking(-1.5)
                        .foregroundStyle(.white)
                        .contentTransition(.numericText())
                        .animation(.spring(response: 0.5, dampingFraction: 0.8), value: account.portfolioValue)
                        .scaleEffect(breathe ? 1.008 : 1.0)
                        .animation(.easeInOut(duration: 3.5).repeatForever(autoreverses: true), value: breathe)

                    HStack(spacing: 8) {
                        PLBadge(value: account.dayPl, percentValue: account.dayPlPercent)
                        LiveDot(color: account.dayPl >= 0 ? LakshmiTheme.positive : LakshmiTheme.negative)
                    }
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 28)
                .padding(.bottom, 24)
                .onAppear { breathe = true }
            }

            // ── Inline chart on dark bg ────────────────────────────────────
            InlineChart(points: points, isPositive: isPositive, onDark: true)
                .frame(height: 170)
                .padding(.horizontal, 4)

            // ── Period picker ──────────────────────────────────────────────
            HStack(spacing: 4) {
                ForEach(periods, id: \.self) { p in
                    Button {
                        withAnimation(LakshmiTheme.springSnappy) {
                            selectedPeriod = p
                            onPeriodChange?(p)
                        }
                    } label: {
                        Text(p)
                            .font(.caption.weight(.semibold))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                            .foregroundStyle(selectedPeriod == p ? .white : .white.opacity(0.40))
                            .background {
                                if selectedPeriod == p {
                                    RoundedRectangle(cornerRadius: 7)
                                        .fill(LakshmiTheme.brandGradient)
                                        .matchedGeometryEffect(id: "periodBG", in: periodNS)
                                }
                            }
                    }
                    .buttonStyle(PressScaleButtonStyle(scale: 0.94))
                }
            }
            .padding(4)
            .background(Color.white.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .padding(.horizontal, LakshmiTheme.pagePad)
            .padding(.top, 10)
            .padding(.bottom, 16)
        }
        .background {
            ZStack {
                LinearGradient(
                    colors: [
                        Color(red: 0.14, green: 0.08, blue: 0.01),
                        Color(red: 0.08, green: 0.04, blue: 0.00),
                    ],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
                RoundedRectangle(cornerRadius: LakshmiTheme.radius)
                    .stroke(LakshmiTheme.brandGradient.opacity(0.40), lineWidth: 1)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radius))
        .shadow(color: LakshmiTheme.gold.opacity(0.35), radius: 24, x: 0, y: 10)
    }

    private func formatCurrency(_ v: Double) -> String {
        let f = NumberFormatter(); f.numberStyle = .currency; f.maximumFractionDigits = 0
        return f.string(from: NSNumber(value: v)) ?? "$\(Int(v))"
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Animated orb background
// ─────────────────────────────────────────────────────────────────────────────

private struct AnimatedOrbBackground: View {
    @State private var move = false

    var body: some View {
        ZStack {
            Circle()
                .fill(LakshmiTheme.gold.opacity(0.22))
                .frame(width: 340, height: 340)
                .blur(radius: 100)
                .offset(x: move ? 90 : -60, y: move ? -50 : 40)
                .animation(.easeInOut(duration: 7).repeatForever(autoreverses: true), value: move)

            Circle()
                .fill(LakshmiTheme.amber.opacity(0.16))
                .frame(width: 280, height: 280)
                .blur(radius: 90)
                .offset(x: move ? -100 : 70, y: move ? 60 : -55)
                .animation(.easeInOut(duration: 9).repeatForever(autoreverses: true).delay(1.5), value: move)

            Circle()
                .fill(LakshmiTheme.saffron.opacity(0.12))
                .frame(width: 220, height: 220)
                .blur(radius: 80)
                .offset(x: move ? 30 : -40, y: move ? -70 : 30)
                .animation(.easeInOut(duration: 6).repeatForever(autoreverses: true).delay(0.8), value: move)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear { move = true }
        .allowsHitTesting(false)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Inline chart
// ─────────────────────────────────────────────────────────────────────────────

private struct InlineChart: View {
    let points: [PortfolioPoint]
    let isPositive: Bool
    var onDark: Bool = false

    private var lineColor: Color { isPositive ? LakshmiTheme.positive : LakshmiTheme.negative }
    private var labelColor: Color { onDark ? .white.opacity(0.35) : .secondary }
    private var gridColor: Color  { onDark ? .white.opacity(0.12) : Color(.separator).opacity(0.4) }
    private var areaOpacity: Double { onDark ? 0.38 : 0.20 }

    var body: some View {
        if points.isEmpty {
            Text("No history yet")
                .foregroundStyle(onDark ? .white.opacity(0.40) : .secondary)
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
                        colors: [lineColor.opacity(areaOpacity), .clear],
                        startPoint: .top, endPoint: .bottom))
                    .interpolationMethod(.catmullRom)
            }
            .chartXAxis(.hidden)
            .chartYAxis {
                AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { v in
                    AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5, dash: [4]))
                        .foregroundStyle(gridColor)
                    AxisValueLabel {
                        if let val = v.as(Double.self) {
                            Text("$\(Int(val / 1000))k").font(.caption2).foregroundStyle(labelColor)
                        }
                    }
                }
            }
            .shadow(color: lineColor.opacity(0.45), radius: 8, x: 0, y: 2)
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Glass stat card (3-up row)
// ─────────────────────────────────────────────────────────────────────────────

private struct GlassStatCard: View {
    let icon: String
    let label: String
    let value: String
    let color: Color
    @State private var appeared = false

    var body: some View {
        VStack(spacing: 6) {
            ZStack {
                Circle().fill(color.opacity(0.12)).frame(width: 34, height: 34)
                Image(systemName: icon)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(color)
            }
            Text(value)
                .font(.system(size: 13, weight: .bold, design: .rounded))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(label)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 12)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm))
        .overlay(
            RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm)
                .stroke(color.opacity(0.15), lineWidth: 1)
        )
        .scaleEffect(appeared ? 1 : 0.88)
        .opacity(appeared ? 1 : 0)
        .animation(LakshmiTheme.springBouncy, value: appeared)
        .onAppear { appeared = true }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Positions section
// ─────────────────────────────────────────────────────────────────────────────

private struct PositionsSection: View {
    let positions: [Position]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Positions")
                    .font(.title3.weight(.bold))
                if !positions.isEmpty {
                    Text("\(positions.count)")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.white)
                        .frame(width: 20, height: 20)
                        .background(LakshmiTheme.brandGradient)
                        .clipShape(Circle())
                }
                Spacer()
            }

            if positions.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "chart.pie")
                        .font(.system(size: 36))
                        .foregroundStyle(LakshmiTheme.gold.opacity(0.35))
                    Text("No open positions")
                        .foregroundStyle(.secondary)
                        .font(.subheadline)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 32)
                .background(.ultraThinMaterial)
                .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radius))
            } else {
                VStack(spacing: 8) {
                    ForEach(Array(positions.enumerated()), id: \.element.id) { index, pos in
                        PositionRowView(position: pos)
                            .cardAppear(delay: Double(index) * 0.05)
                    }
                }
            }
        }
    }
}
