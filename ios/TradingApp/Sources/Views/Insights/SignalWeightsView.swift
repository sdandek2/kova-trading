import SwiftUI

// MARK: - Signal Weights Section

struct SignalWeightsView: View {
    @ObservedObject var vm: InsightsViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Header
            HStack {
                Text("Signal Intelligence")
                    .font(.headline)
                Spacer()
                if vm.isLoadingSignalWeights {
                    ProgressView().scaleEffect(0.7)
                } else {
                    Button {
                        Task { await vm.loadSignalWeights() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.horizontal)

            if vm.signalWeights.isEmpty && !vm.isLoadingSignalWeights {
                Text("No signal data yet — weights update every Sunday after 15+ trades.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal)
            } else {
                VStack(spacing: 8) {
                    ForEach(vm.signalWeights) { signal in
                        SignalWeightRow(signal: signal)
                    }
                }
                .padding(.horizontal)
            }
        }
        .task { await vm.loadSignalWeights() }
    }
}

// MARK: - Row

struct SignalWeightRow: View {
    let signal: SignalWeight
    @State private var showHistory = false

    var trendColor: Color {
        switch signal.trend {
        case "up":   return .green
        case "down": return .red
        default:     return .secondary
        }
    }

    var trendIcon: String {
        switch signal.trend {
        case "up":   return "arrow.up.circle.fill"
        case "down": return "arrow.down.circle.fill"
        default:     return "minus.circle.fill"
        }
    }

    var barColor: Color {
        let pct = signal.pctOfDefault
        if pct >= 120 { return .green }
        if pct >= 80  { return .blue }
        if pct >= 50  { return .orange }
        return .red
    }

    var body: some View {
        Button { showHistory.toggle() } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    // Trend icon
                    Image(systemName: trendIcon)
                        .foregroundStyle(trendColor)
                        .font(.system(size: 14))

                    // Signal name
                    Text(signal.displayName)
                        .font(.subheadline)
                        .foregroundStyle(.primary)

                    Spacer()

                    // Weight fraction
                    Text("\(signal.currentWeight) / \(signal.defaultWeight)")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)

                    // Percentage
                    Text("\(signal.pctOfDefault)%")
                        .font(.caption.bold())
                        .foregroundStyle(barColor)
                        .frame(width: 40, alignment: .trailing)
                }

                // Progress bar
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        RoundedRectangle(cornerRadius: 3)
                            .fill(Color(.systemFill))
                            .frame(height: 5)

                        // Default marker at 100% position (bar max = 150% of default, so marker at 2/3)
                        Rectangle()
                            .fill(Color.secondary.opacity(0.4))
                            .frame(width: 1, height: 9)
                            .offset(x: geo.size.width * (2.0 / 3.0), y: -2)

                        // Current weight bar (cap display at 150%)
                        let fillPct = min(1.0, CGFloat(signal.currentWeight) / CGFloat(signal.defaultWeight * 3 / 2))
                        RoundedRectangle(cornerRadius: 3)
                            .fill(barColor)
                            .frame(width: geo.size.width * fillPct, height: 5)
                    }
                }
                .frame(height: 5)
            }
            .padding(10)
            .background(Color(.secondarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .sheet(isPresented: $showHistory) {
            SignalHistorySheet(signal: signal)
        }
    }
}

// MARK: - History Sheet

struct SignalHistorySheet: View {
    let signal: SignalWeight
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    LabeledContent("Current Weight", value: "\(signal.currentWeight)")
                    LabeledContent("Default Weight", value: "\(signal.defaultWeight)")
                    LabeledContent("% of Default", value: "\(signal.pctOfDefault)%")
                    if let reason = signal.adjustmentReason {
                        LabeledContent("Last Reason", value: reason)
                    }
                    if let adj = signal.lastAdjusted {
                        LabeledContent("Last Adjusted", value: adj)
                    }
                } header: {
                    Text("Current State")
                }

                Section {
                    if signal.history.isEmpty {
                        Text("No adjustments yet — needs 15+ trades per week to activate.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(signal.history.reversed()) { entry in
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(entry.adjustedAt ?? "")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                    if let wr = entry.winRate {
                                        Text("Win rate \(Int(wr * 100))% over \(entry.sampleCount ?? 0) trades")
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                                Spacer()
                                HStack(spacing: 4) {
                                    Text("\(entry.oldWeight)")
                                        .foregroundStyle(.secondary)
                                    Image(systemName: "arrow.right")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                    Text("\(entry.newWeight)")
                                        .fontWeight(.semibold)
                                        .foregroundStyle(entry.direction == "up" ? .green : entry.direction == "down" ? .red : .primary)
                                }
                                .font(.subheadline.monospacedDigit())
                            }
                        }
                    }
                } header: {
                    Text("Adjustment History (last 8 weeks)")
                }
            }
            .navigationTitle(signal.displayName)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}
