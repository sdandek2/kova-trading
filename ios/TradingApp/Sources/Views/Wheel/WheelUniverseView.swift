import SwiftUI

// ── Wheel Universe View ───────────────────────────────────────────────────────
// Shows AI-discovered stocks for the wheel strategy.
// Scores, IV profiles, reasons — refreshed every Sunday 8 PM ET by AI.
// Shows top 5 by default with "Show all" toggle to avoid excessive scrolling.

struct WheelUniverseView: View {
    @ObservedObject var vm: WheelViewModel
    @State private var showAll = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // ── Header ────────────────────────────────────────────────────
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text("AI Universe")
                            .font(.headline.weight(.bold))
                        if let count = vm.status?.universeCount, count > 0 {
                            Text("\(count) stocks")
                                .font(.caption.weight(.semibold))
                                .padding(.horizontal, 7).padding(.vertical, 2)
                                .background(LakshmiTheme.purple.opacity(0.12))
                                .foregroundStyle(LakshmiTheme.purple)
                                .clipShape(Capsule())
                        }
                    }
                    Text("Auto-refreshed every Sunday 8 PM ET")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    Task { await vm.refreshUniverse() }
                } label: {
                    if vm.isRefreshingUniverse {
                        ProgressView().scaleEffect(0.8)
                    } else {
                        Label("Refresh", systemImage: "sparkles")
                            .font(.caption.weight(.semibold))
                    }
                }
                .buttonStyle(.bordered)
                .tint(LakshmiTheme.purple)
                .disabled(vm.isRefreshingUniverse)
            }

            // ── Score legend ──────────────────────────────────────────────
            HStack(spacing: 12) {
                ScoreLegendItem(color: LakshmiTheme.positive, label: "≥80 Strong")
                ScoreLegendItem(color: .orange,            label: "60-79 Good")
                ScoreLegendItem(color: .secondary,         label: "IV: options richness")
            }
            .padding(.vertical, 4)
            .padding(.horizontal, 8)
            .background(Color.secondary.opacity(0.06))
            .clipShape(RoundedRectangle(cornerRadius: 8))

            // ── Stock list ────────────────────────────────────────────────
            let universe = (vm.status?.universe ?? []).sorted { $0.score > $1.score }
            if universe.isEmpty {
                HStack {
                    Spacer()
                    VStack(spacing: 8) {
                        Image(systemName: "sparkle.magnifyingglass")
                            .font(.system(size: 32))
                            .foregroundStyle(.secondary)
                        Text("No universe yet")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        Text("Tap Refresh to let AI discover stocks")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                    Spacer()
                }
                .padding(.vertical, 24)
            } else {
                let visible = showAll ? universe : Array(universe.prefix(5))
                VStack(spacing: 0) {
                    ForEach(Array(visible.enumerated()), id: \.element.id) { idx, stock in
                        WheelUniverseRowView(stock: stock)
                        if idx < visible.count - 1 {
                            Divider().padding(.leading, 52)
                        }
                    }
                }

                // Show more / collapse button
                if universe.count > 5 {
                    Button {
                        withAnimation(.easeInOut(duration: 0.25)) { showAll.toggle() }
                    } label: {
                        HStack(spacing: 4) {
                            Text(showAll ? "Show less" : "Show all \(universe.count) stocks")
                                .font(.caption.weight(.semibold))
                            Image(systemName: showAll ? "chevron.up" : "chevron.down")
                                .font(.caption2)
                        }
                        .foregroundStyle(LakshmiTheme.purple)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                        .background(LakshmiTheme.purple.opacity(0.06))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                }
            }
        }
        .padding(16)
        .kovaCard()
    }
}

// MARK: - Legend item

private struct ScoreLegendItem: View {
    let color: Color
    let label: String
    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(color.opacity(0.7)).frame(width: 7, height: 7)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: - Universe Row

struct WheelUniverseRowView: View {
    let stock: WheelUniverseStock

    private var scoreColor: Color {
        if stock.score >= 80 { return LakshmiTheme.positive }
        if stock.score >= 60 { return .orange }
        return LakshmiTheme.negative
    }

    private var ivChipColor: Color {
        switch stock.ivProfile?.lowercased() {
        case "very-high": return LakshmiTheme.negative
        case "high":      return LakshmiTheme.blue
        default:          return .secondary
        }
    }

    var body: some View {
        HStack(spacing: 12) {
            // Score badge
            ZStack {
                Circle()
                    .fill(scoreColor.opacity(0.15))
                    .frame(width: 40, height: 40)
                Text("\(stock.score)")
                    .font(.system(size: 13, weight: .bold, design: .rounded))
                    .foregroundStyle(scoreColor)
            }

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(stock.symbol)
                        .font(.subheadline.weight(.bold))
                    if let ivProfile = stock.ivProfile {
                        Text(ivProfile.uppercased())
                            .font(.caption2.weight(.bold))
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(ivChipColor.opacity(0.15))
                            .foregroundStyle(ivChipColor)
                            .clipShape(Capsule())
                    }
                }
                if let reason = stock.reason {
                    Text(reason)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }

            Spacer()

            // Score bar
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Color.secondary.opacity(0.2))
                        .frame(height: 4)
                    RoundedRectangle(cornerRadius: 2)
                        .fill(scoreColor)
                        .frame(width: geo.size.width * Double(stock.score) / 100, height: 4)
                }
            }
            .frame(width: 50, height: 4)
        }
        .padding(.vertical, 8)
    }
}
