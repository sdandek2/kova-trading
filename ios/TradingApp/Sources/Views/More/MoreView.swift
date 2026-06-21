import SwiftUI

struct MoreView: View {
    @State private var showBot = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 20) {
                    // ── Lakshmi AI Bot ─────────────────────────────────────
                    MoreSection(title: "AI Trading Bot") {
                        MoreBotLink()
                    }

                    // ── Trading history ────────────────────────────────────
                    MoreSection(title: "History & Reports") {
                        MoreNavLink(icon: "list.bullet.rectangle", title: "Orders", subtitle: "All placed orders", color: LakshmiTheme.blue) {
                            OrdersView()
                        }
                        MoreNavLink(icon: "chart.bar.xaxis", title: "Trade History", subtitle: "Closed positions + decisions", color: LakshmiTheme.purple) {
                            TradeHistoryView()
                        }
                        MoreNavLink(icon: "chart.bar.doc.horizontal", title: "Daily Report", subtitle: "End-of-day AI summary", color: .indigo) {
                            EODReportView()
                        }
                    }

                    // ── Performance ────────────────────────────────────────
                    MoreSection(title: "Performance") {
                        MoreNavLink(icon: "chart.line.uptrend.xyaxis", title: "Performance", subtitle: "Equity curve & metrics", color: LakshmiTheme.positive) {
                            PerformanceView().navigationTitle("Performance")
                        }
                        MoreNavLink(icon: "chart.bar.fill", title: "Analytics", subtitle: "Scorecards & signal breakdown", color: .teal) {
                            AnalyticsView()
                        }
                    }

                    // ── Intelligence ───────────────────────────────────────
                    MoreSection(title: "Signal Intelligence") {
                        MoreNavLink(icon: "waveform.path.ecg", title: "Signal Weights", subtitle: "Which signals are pulling the most weight", color: .cyan) {
                            ScrollView {
                                VStack(spacing: 16) {
                                    SignalWeightsViewStandalone()
                                    ConnectorHealthViewStandalone()
                                }
                                .padding()
                            }
                            .navigationTitle("Signal Intelligence")
                        }
                    }

                    // ── Finance ────────────────────────────────────────────
                    MoreSection(title: "Finance") {
                        MoreNavLink(icon: "dollarsign.circle", title: "Estimated Taxes", subtitle: "Short-term capital gains estimate", color: .orange) {
                            TaxSummaryView()
                        }
                        MoreNavLink(icon: "banknote", title: "Profit Reserve", subtitle: "Set aside profit from trading", color: LakshmiTheme.positive) {
                            ProfitReserveView()
                        }
                    }

                    // ── Console ────────────────────────────────────────────
                    MoreSection(title: "Developer") {
                        MoreNavLink(icon: "terminal.fill", title: "Console", subtitle: "Health monitor · log stream · quick fetch", color: LakshmiTheme.cyan) {
                            ConsoleView()
                        }
                    }

                    // ── App ────────────────────────────────────────────────
                    MoreSection(title: "App") {
                        MoreNavLink(icon: "gearshape.fill", title: "Settings", subtitle: "Appearance & preferences", color: .gray) {
                            SettingsView()
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .padding(.bottom, 120)
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 6) {
                        Image(systemName: "ellipsis.circle.fill")
                            .foregroundStyle(LakshmiTheme.brandGradient)
                        Text("More").font(.headline.weight(.bold))
                    }
                }
            }
        }
    }
}

// MARK: - Section wrapper

private struct MoreSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.leading, 4)

            VStack(spacing: 1) {
                content()
            }
            .background(LakshmiTheme.card)
            .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm))
        }
    }
}

// MARK: - Nav link row

private struct MoreNavLink<Destination: View>: View {
    let icon: String
    let title: String
    let subtitle: String
    let color: Color
    @ViewBuilder let destination: () -> Destination

    var body: some View {
        NavigationLink(destination: destination()) {
            HStack(spacing: 13) {
                ZStack {
                    RoundedRectangle(cornerRadius: 8)
                        .fill(color.opacity(0.13))
                        .frame(width: 36, height: 36)
                    Image(systemName: icon)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(color)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                    Text(subtitle).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
        }
        .buttonStyle(PressScaleButtonStyle(scale: 0.97))
    }
}

// Bot link uses fullScreenCover so BotTabView's inner NavigationStack gets its own modal context
private struct MoreBotLink: View {
    @State private var show = false

    var body: some View {
        Button { show = true } label: {
            HStack(spacing: 13) {
                ZStack {
                    RoundedRectangle(cornerRadius: 8)
                        .fill(LakshmiTheme.purple.opacity(0.13))
                        .frame(width: 36, height: 36)
                    Image(systemName: "brain")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(LakshmiTheme.purple)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Lakshmi Bot")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                    Text("AI trading decisions · status · config")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "arrow.up.right.square")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
        }
        .buttonStyle(PressScaleButtonStyle(scale: 0.97))
        .fullScreenCover(isPresented: $show) { BotTabView() }
    }
}

// Thin wrappers so SignalWeights/ConnectorHealth work without injecting the VM at this level
private struct SignalWeightsViewStandalone: View {
    @StateObject private var vm = InsightsViewModel()
    var body: some View {
        SignalWeightsView(vm: vm)
            .task { await vm.loadSignalWeights() }
    }
}

private struct ConnectorHealthViewStandalone: View {
    @StateObject private var vm = InsightsViewModel()
    var body: some View {
        ConnectorHealthView(vm: vm)
            .task { await vm.loadConnectorHealth() }
    }
}
