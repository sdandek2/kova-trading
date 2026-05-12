import SwiftUI

struct AIView: View {
    @StateObject private var vm = TradingViewModel()
    @State private var circuitBreakerDayPl: Double? = nil
    @State private var circuitBreakerLimit: Double = 3.0

    var body: some View {
        NavigationStack {
            Group {
                if vm.isLoading && vm.status == nil {
                    LoadingView()
                } else if let error = vm.errorMessage, vm.status == nil {
                    ErrorView(message: error) { await vm.load() }
                } else {
                    ScrollView {
                        VStack(spacing: 16) {
                            // ── Circuit breaker banner (only shown when active) ──
                            if let dayPl = circuitBreakerDayPl, dayPl < -circuitBreakerLimit {
                                CircuitBreakerBanner(dayPlPercent: dayPl, limitPct: circuitBreakerLimit)
                            }

                            PreMarketView()

                            // ── Active short positions (only shown when bot has open shorts) ──
                            OpenShortsCard()

                            BotControlView(vm: vm)

                            StrategyPickerView()

                            TradingFloorView()

                            WatchlistEditorView()

                            // ── Real-time bot activity log ──
                            BotActivityView()

                            // ── Navigation buttons ──
                            HStack(spacing: 12) {
                                NavigationLink {
                                    TradeHistoryView()
                                } label: {
                                    Label("Trade History", systemImage: "chart.bar.xaxis")
                                        .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(.bordered)

                                NavigationLink {
                                    PerformanceView()
                                        .navigationTitle("Performance")
                                } label: {
                                    Label("Performance", systemImage: "chart.line.uptrend.xyaxis")
                                        .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(.borderedProminent)
                            }

                            // ── Latest AI decision ──
                            if let analysis = vm.analysis {
                                VStack(alignment: .leading, spacing: 12) {
                                    HStack {
                                        Text("Latest AI Decision")
                                            .font(.headline)
                                        Spacer()
                                        if let ts = analysis.timestamp {
                                            Text(ts, style: .relative)
                                                .font(.caption)
                                                .foregroundStyle(.secondary)
                                        }
                                    }

                                    if let action = analysis.lastAction, let symbol = analysis.symbol, action != "hold" {
                                        HStack(spacing: 8) {
                                            Text(action.uppercased())
                                                .font(.caption)
                                                .fontWeight(.bold)
                                                .foregroundStyle(.white)
                                                .padding(.horizontal, 8)
                                                .padding(.vertical, 4)
                                                .background(action == "buy" ? Color.green : Color.red)
                                                .clipShape(Capsule())
                                            Text(symbol)
                                                .font(.subheadline)
                                                .fontWeight(.medium)
                                        }
                                    }

                                    Text(analysis.reasoning)
                                        .font(.subheadline)
                                        .foregroundStyle(.primary)
                                        .lineSpacing(4)
                                }
                                .padding()
                                .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
                            } else {
                                VStack(spacing: 8) {
                                    Image(systemName: "brain")
                                        .font(.system(size: 32))
                                        .foregroundStyle(.secondary)
                                    Text("No analysis yet")
                                        .font(.subheadline)
                                        .foregroundStyle(.secondary)
                                    Text("Start the bot to see Claude's trading decisions here.")
                                        .font(.caption)
                                        .foregroundStyle(.tertiary)
                                        .multilineTextAlignment(.center)
                                }
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 40)
                            }
                        }
                        .padding()
                    }
                    .refreshable { await vm.load() }
                }
            }
            .navigationTitle("AI Agent")
        }
        .task {
            await vm.load()
            await loadCircuitBreakerStatus()
        }
        .onReceive(NotificationCenter.default.publisher(for: .circuitBreakerFired)) { note in
            if let dayPl = note.userInfo?["day_pl_percent"] as? Double {
                circuitBreakerDayPl = dayPl
            }
        }
    }

    private func loadCircuitBreakerStatus() async {
        // Check account P&L and risk settings to set circuit breaker state
        if let riskSettings = try? await APIService.shared.getRiskSettings() {
            circuitBreakerLimit = riskSettings.daily_loss_limit_pct
        }
        // Account P&L comes from the dashboard VM; rely on WebSocket for live updates
    }
}

extension Notification.Name {
    static let circuitBreakerFired = Notification.Name("circuitBreakerFired")
}

// MARK: - Open Shorts Card

struct OpenShortsCard: View {
    @State private var shorts: [Position] = []
    @State private var isLoading = true

    var body: some View {
        Group {
            if !isLoading && !shorts.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    // Header
                    HStack(spacing: 10) {
                        ZStack {
                            Circle().fill(Color.orange.opacity(0.15)).frame(width: 36, height: 36)
                            Image(systemName: "arrow.down.circle.fill")
                                .foregroundStyle(.orange).font(.system(size: 16))
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Active Short Positions")
                                .font(.headline).foregroundStyle(.primary)
                            Text("\(shorts.count) open short\(shorts.count == 1 ? "" : "s")")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        // Total unrealized P&L across all shorts
                        let totalPl = shorts.reduce(0) { $0 + $1.unrealizedPl }
                        Text("\(totalPl >= 0 ? "+" : "")$\(String(format: "%.0f", totalPl))")
                            .font(.subheadline).fontWeight(.bold)
                            .foregroundStyle(totalPl >= 0 ? .green : .red)
                    }
                    .padding()

                    Divider().padding(.horizontal)

                    ForEach(shorts) { position in
                        ShortPositionRow(position: position)
                        if position.id != shorts.last?.id {
                            Divider().padding(.leading, 52)
                        }
                    }
                    .padding(.bottom, 8)
                }
                .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
            }
        }
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        isLoading = true
        let positions = (try? await APIService.shared.getPositions()) ?? []
        shorts = positions.filter { $0.isShort }
        isLoading = false
    }
}

struct ShortPositionRow: View {
    let position: Position

    var body: some View {
        HStack(spacing: 12) {
            // Direction indicator
            RoundedRectangle(cornerRadius: 3)
                .fill(Color.orange)
                .frame(width: 4)
                .padding(.vertical, 8)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(position.symbol)
                        .font(.subheadline).fontWeight(.semibold)
                    Text("SHORT")
                        .font(.caption2).fontWeight(.bold)
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Color.orange)
                        .clipShape(Capsule())
                    Spacer()
                    // P&L %
                    Text("\(position.unrealizedPlPercent >= 0 ? "+" : "")\(String(format: "%.1f", position.unrealizedPlPercent))%")
                        .font(.subheadline).fontWeight(.bold)
                        .foregroundStyle(position.unrealizedPlPercent >= 0 ? .green : .red)
                }
                HStack(spacing: 8) {
                    Text("Entry $\(String(format: "%.2f", position.avgEntryPrice)) → Now $\(String(format: "%.2f", position.currentPrice))")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Text("\(Int(position.qty)) shares")
                        .font(.caption2).foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}
