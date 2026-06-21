import SwiftUI

// ── Wheel Bot Tab ─────────────────────────────────────────────────────────────
// Main view for the iOS Wheel tab.
// Shows: hero P&L card, active positions with decay bars, universe, controls.
// Completely separate from Kova Dashboard — different ViewModel, different API calls.

struct WheelView: View {
    @StateObject private var vm = WheelViewModel()
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    // ── Hero Card ─────────────────────────────────────────
                    WheelHeroCard(vm: vm)

                    // ── Active Positions ──────────────────────────────────
                    WheelPositionsSection(vm: vm)

                    // ── Universe ──────────────────────────────────────────
                    WheelUniverseView(vm: vm)

                    // ── Quick Controls ────────────────────────────────────
                    WheelControlsCard(vm: vm)

                    Spacer(minLength: 24)
                }
                .padding(.horizontal, 16)
                .padding(.top, 8)
            }
            .background(LakshmiTheme.pageBackground.ignoresSafeArea())
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 6) {
                        Text("⚙️").font(.system(size: 15))
                        Text("Wheel Bot").font(.headline.weight(.bold))
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await vm.loadStatus() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 15))
                    }
                    .disabled(vm.isLoading)
                }
            }
            .refreshable {
                await vm.loadStatus()
            }
            .task {
                await vm.loadStatus()
            }
            .alert("Wheel", isPresented: $vm.showCycleResult) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(vm.cycleResult ?? "")
            }
            .overlay {
                if vm.isLoading && vm.status == nil {
                    ProgressView("Loading wheel…")
                        .padding(24)
                        .background(.regularMaterial)
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                }
            }
        }
    }
}

// MARK: - Hero Card

private struct WheelHeroCard: View {
    @ObservedObject var vm: WheelViewModel
    @State private var appeared = false

    var body: some View {
        VStack(spacing: 0) {
            // ── Dark hero — same language as Lakshmi + PureAI ──────────────
            ZStack {
                LinearGradient(
                    colors: [
                        Color(red: 0.14, green: 0.08, blue: 0.01),
                        Color(red: 0.08, green: 0.04, blue: 0.00),
                    ],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
                WheelOrbBackground()

                VStack(spacing: 8) {
                    Text("WHEEL BOT")
                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                        .tracking(3)
                        .foregroundStyle(.white.opacity(0.38))

                    Text(vm.totalPremiumDisplay)
                        .font(.system(size: 48, weight: .bold, design: .rounded))
                        .tracking(-1.5)
                        .foregroundStyle(.white)
                        .contentTransition(.numericText())

                    HStack(spacing: 6) {
                        Text("Realized P&L:")
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.white.opacity(0.50))
                        Text(vm.realizedPlDisplay)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(LakshmiTheme.positive)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 5)
                    .background(.white.opacity(0.08))
                    .clipShape(Capsule())
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 28)
                .padding(.bottom, 24)
            }

            // ── Account row ─────────────────────────────────────────────────
            if let acct = vm.status?.account {
                Rectangle()
                    .fill(LakshmiTheme.brandGradient.opacity(0.30))
                    .frame(height: 1)

                HStack(spacing: 0) {
                    WheelStatCell(label: "Portfolio",    value: String(format: "$%.0f", acct.portfolioValue), icon: "dollarsign.circle.fill")
                    Rectangle().fill(.white.opacity(0.10)).frame(width: 1, height: 28)
                    WheelStatCell(label: "Cash",         value: String(format: "$%.0f", acct.cash),          icon: "banknote.fill")
                    Rectangle().fill(.white.opacity(0.10)).frame(width: 1, height: 28)
                    WheelStatCell(label: "Buying Power", value: String(format: "$%.0f", acct.buyingPower),   icon: "bolt.fill")
                }
                .padding(.vertical, 10)
                .background(.white.opacity(0.04))
            }

            // ── Stats row ────────────────────────────────────────────────────
            Rectangle()
                .fill(LakshmiTheme.brandGradient.opacity(0.25))
                .frame(height: 1)

            HStack(spacing: 0) {
                WheelStatCell(label: "Positions",  value: vm.slotsDisplay,                                                                             icon: "chart.bar.fill")
                Rectangle().fill(.white.opacity(0.10)).frame(width: 1, height: 28)
                WheelStatCell(label: "Completed",  value: "\(vm.status?.summary.completedCycles ?? 0)",                                                icon: "checkmark.circle.fill")
                Rectangle().fill(.white.opacity(0.10)).frame(width: 1, height: 28)
                WheelStatCell(label: "Win Rate",   value: vm.status?.summary.winRate.map { String(format: "%.0f%%", $0) } ?? "—",                    icon: "trophy.fill")
                Rectangle().fill(.white.opacity(0.10)).frame(width: 1, height: 28)
                WheelStatCell(label: "Reserve",    value: vm.status.flatMap { $0.profitReserve }.map { String(format: "$%.0f", $0) } ?? "$0",         icon: "banknote.fill")
            }
            .padding(.vertical, 12)
            .background(.white.opacity(0.03))
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
                    .stroke(LakshmiTheme.brandGradient.opacity(0.35), lineWidth: 1)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radius))
        .shadow(color: LakshmiTheme.gold.opacity(0.32), radius: 22, x: 0, y: 10)
        .scaleEffect(appeared ? 1 : 0.95)
        .opacity(appeared ? 1 : 0)
        .animation(.spring(response: 0.5, dampingFraction: 0.82), value: appeared)
        .onAppear { appeared = true }
    }
}

private struct WheelOrbBackground: View {
    @State private var move = false

    var body: some View {
        ZStack {
            Circle()
                .fill(LakshmiTheme.gold.opacity(0.22))
                .frame(width: 320, height: 320)
                .blur(radius: 100)
                .offset(x: move ? 90 : -60, y: move ? -50 : 40)
                .animation(.easeInOut(duration: 7).repeatForever(autoreverses: true), value: move)

            Circle()
                .fill(LakshmiTheme.amber.opacity(0.16))
                .frame(width: 260, height: 260)
                .blur(radius: 90)
                .offset(x: move ? -100 : 70, y: move ? 60 : -55)
                .animation(.easeInOut(duration: 9).repeatForever(autoreverses: true).delay(1.5), value: move)

            Circle()
                .fill(LakshmiTheme.saffron.opacity(0.12))
                .frame(width: 200, height: 200)
                .blur(radius: 80)
                .offset(x: move ? 30 : -40, y: move ? -70 : 30)
                .animation(.easeInOut(duration: 6).repeatForever(autoreverses: true).delay(0.8), value: move)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear { move = true }
        .allowsHitTesting(false)
    }
}

private struct WheelStatCell: View {
    let label: String
    let value: String
    let icon: String

    var body: some View {
        VStack(spacing: 4) {
            Image(systemName: icon)
                .font(.system(size: 13))
                .foregroundStyle(LakshmiTheme.gold.opacity(0.80))
            Text(value)
                .font(.system(size: 14, weight: .bold, design: .rounded))
                .foregroundStyle(.white)
            Text(label)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(.white.opacity(0.42))
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Positions Section

private struct WheelPositionsSection: View {
    @ObservedObject var vm: WheelViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Active Positions")
                    .font(.headline.weight(.bold))
                Spacer()
                Text(vm.slotsDisplay)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }

            let positions = vm.status?.activePositions ?? []
            if positions.isEmpty {
                HStack {
                    Spacer()
                    VStack(spacing: 8) {
                        Image(systemName: "chart.line.downtrend.xyaxis")
                            .font(.system(size: 28))
                            .foregroundStyle(.secondary)
                        Text("No active positions")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        Text("Wheel will open puts on next scan day")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                    Spacer()
                }
                .padding(.vertical, 20)
                .kovaCard()
            } else {
                VStack(spacing: 0) {
                    ForEach(positions) { position in
                        WheelPositionRowView(position: position)
                        if position.id != positions.last?.id {
                            Divider().padding(.leading, 56)
                        }
                    }
                }
                .padding(16)
                .kovaCard()
            }
        }
    }
}

// MARK: - Controls Card

private struct WheelControlsCard: View {
    @ObservedObject var vm: WheelViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Controls")
                .font(.headline.weight(.bold))

            HStack(spacing: 10) {
                // Scan (preview only)
                Button {
                    Task { await vm.scanOpportunities() }
                } label: {
                    Label(
                        vm.isScanningOpps ? "Scanning…" : "Scan Now",
                        systemImage: "magnifyingglass"
                    )
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .tint(LakshmiTheme.gold)
                .disabled(vm.isScanningOpps || vm.isRunningCycle)

                // Run full cycle
                Button {
                    Task { await vm.runCycle() }
                } label: {
                    if vm.isRunningCycle {
                        HStack(spacing: 6) {
                            ProgressView().scaleEffect(0.8)
                            Text("Running…")
                        }
                        .frame(maxWidth: .infinity)
                    } else {
                        Label("Run Cycle", systemImage: "play.circle.fill")
                            .font(.subheadline.weight(.semibold))
                            .frame(maxWidth: .infinity)
                    }
                }
                .buttonStyle(.borderedProminent)
                .tint(LakshmiTheme.amber)
                .disabled(vm.isRunningCycle || vm.isScanningOpps)
            }

            // Scan opportunities preview (if loaded)
            if !vm.opportunities.isEmpty {
                Divider()
                Text("Opportunities (\(vm.opportunities.count))")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
                ForEach(vm.opportunities.prefix(5)) { opp in
                    WheelOpportunityRow(opp: opp)
                }
            }

            // Reserve withdraw (shown only when balance > 0)
            let reserve = vm.status?.profitReserve ?? 0
            if reserve > 0 {
                Divider()
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Profit Reserve")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                        Text(String(format: "$%.2f set aside", reserve))
                            .font(.subheadline.weight(.bold))
                            .foregroundStyle(.green)
                    }
                    Spacer()
                    Button {
                        Task { await vm.withdrawReserve() }
                    } label: {
                        HStack(spacing: 4) {
                            if vm.isWithdrawingReserve {
                                ProgressView().scaleEffect(0.7)
                            } else {
                                Image(systemName: "arrow.down.circle")
                            }
                            Text("Withdraw")
                        }
                        .font(.caption.weight(.semibold))
                    }
                    .buttonStyle(.bordered)
                    .tint(.green)
                    .disabled(vm.isWithdrawingReserve)
                }
                if let msg = vm.withdrawReserveSuccess {
                    Text(msg).font(.caption).foregroundStyle(.green)
                }
            }

            // Schedule info
            Divider()
            VStack(alignment: .leading, spacing: 4) {
                Label("Auto-runs Mon–Fri 9:45 AM ET", systemImage: "clock.fill")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Label("Universe refresh: Sunday 8 PM ET", systemImage: "sparkles")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Label("Optimizer: Friday 4:30 PM ET", systemImage: "brain")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .kovaCard()
    }
}

// MARK: - Opportunity Row

private struct WheelOpportunityRow: View {
    let opp: WheelOpportunity

    var body: some View {
        HStack(spacing: 10) {
            Text(opp.symbol)
                .font(.subheadline.weight(.bold))
                .frame(width: 48, alignment: .leading)

            VStack(alignment: .leading, spacing: 2) {
                Text("$\(opp.strike, specifier: "%.2f") put · DTE \(opp.dte)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let iv = opp.iv {
                    Text("IV \(iv, specifier: "%.0f")%")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }

            Spacer()

            VStack(alignment: .trailing, spacing: 2) {
                Text(String(format: "%.1f%%", opp.annualYieldPct) + " annual")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(LakshmiTheme.positive)
                Text(String(format: "$%.2f prem", opp.premium))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
    }
}
