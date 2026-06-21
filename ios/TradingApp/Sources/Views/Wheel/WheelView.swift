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
            .navigationTitle("Wheel Bot")
            .navigationBarTitleDisplayMode(.large)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    HStack(spacing: 8) {
                        // Paper/Live badge
                        if let mode = vm.status?.mode {
                            Text(mode.uppercased())
                                .font(.caption2.weight(.bold))
                                .padding(.horizontal, 8).padding(.vertical, 3)
                                .background(mode == "live" ? LakshmiTheme.positive.opacity(0.2) : Color.orange.opacity(0.2))
                                .foregroundStyle(mode == "live" ? LakshmiTheme.positive : .orange)
                                .clipShape(Capsule())
                        }
                        Button {
                            Task { await vm.loadStatus() }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                                .font(.system(size: 15))
                        }
                        .disabled(vm.isLoading)
                    }
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

    var body: some View {
        VStack(spacing: 0) {
            // Gradient header
            LinearGradient(
                colors: [LakshmiTheme.purple, LakshmiTheme.blue],
                startPoint: .topLeading, endPoint: .bottomTrailing
            )
            .frame(height: 120)
            .overlay {
                VStack(spacing: 6) {
                    Text("Total Premium Collected")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.8))
                    Text(vm.totalPremiumDisplay)
                        .font(.system(size: 36, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                    Text("Realized P&L: \(vm.realizedPlDisplay)")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.85))
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 20))

            // Account balance row
            if let acct = vm.status?.account {
                HStack(spacing: 0) {
                    WheelStatCell(
                        label: "Portfolio",
                        value: String(format: "$%.0f", acct.portfolioValue),
                        icon: "dollarsign.circle.fill"
                    )
                    Divider().frame(height: 36)
                    WheelStatCell(
                        label: "Cash",
                        value: String(format: "$%.0f", acct.cash),
                        icon: "banknote.fill"
                    )
                    Divider().frame(height: 36)
                    WheelStatCell(
                        label: "Buying Power",
                        value: String(format: "$%.0f", acct.buyingPower),
                        icon: "bolt.fill"
                    )
                }
                .padding(.vertical, 10)
                .background(LakshmiTheme.purple.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .padding(.horizontal, 4)
            }

            // Stats row
            HStack(spacing: 0) {
                WheelStatCell(
                    label: "Positions",
                    value: vm.slotsDisplay,
                    icon: "chart.bar.fill"
                )
                Divider().frame(height: 36)
                WheelStatCell(
                    label: "Completed",
                    value: "\(vm.status?.summary.completedCycles ?? 0)",
                    icon: "checkmark.circle.fill"
                )
                Divider().frame(height: 36)
                WheelStatCell(
                    label: "Win Rate",
                    value: vm.status?.summary.winRate.map { String(format: "%.0f%%", $0) } ?? "—",
                    icon: "trophy.fill"
                )
                Divider().frame(height: 36)
                WheelStatCell(
                    label: "Reserve",
                    value: vm.status.flatMap { $0.profitReserve }.map { String(format: "$%.0f", $0) } ?? "$0",
                    icon: "banknote.fill"
                )
            }
            .padding(.vertical, 12)
            .background(Color(.secondarySystemGroupedBackground))
            .clipShape(RoundedRectangle(cornerRadius: 16))
            .padding(.top, -8)
        }
    }
}

private struct WheelStatCell: View {
    let label: String
    let value: String
    let icon: String

    var body: some View {
        VStack(spacing: 4) {
            Image(systemName: icon)
                .font(.system(size: 14))
                .foregroundStyle(LakshmiTheme.purple)
            Text(value)
                .font(.system(size: 15, weight: .bold, design: .rounded))
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
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
                .tint(LakshmiTheme.blue)
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
                .tint(LakshmiTheme.purple)
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
