import SwiftUI

// ── Wheel Universe View ───────────────────────────────────────────────────────
// Shows AI-discovered stocks for the wheel strategy.
// Scores, IV profiles, reasons — refreshed every Sunday 8 PM ET by AI.

struct WheelUniverseView: View {
    @ObservedObject var vm: WheelViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // ── Header ────────────────────────────────────────────────────
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("AI Universe")
                        .font(.headline.weight(.bold))
                    Text("Auto-refreshed every Sunday 8 PM ET")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    Task { await vm.refreshUniverse() }
                } label: {
                    if vm.isRefreshingUniverse {
                        ProgressView()
                            .scaleEffect(0.8)
                    } else {
                        Label("Refresh", systemImage: "sparkles")
                            .font(.caption.weight(.semibold))
                    }
                }
                .buttonStyle(.bordered)
                .tint(KovaTheme.purple)
                .disabled(vm.isRefreshingUniverse)
            }

            // ── Stock list ────────────────────────────────────────────────
            let universe = vm.status?.universe ?? []
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
                LazyVStack(spacing: 8) {
                    ForEach(universe.sorted { $0.score > $1.score }) { stock in
                        WheelUniverseRowView(stock: stock)
                    }
                }
            }
        }
        .padding(16)
        .kovaCard()
    }
}

// MARK: - Universe Row

struct WheelUniverseRowView: View {
    let stock: WheelUniverseStock

    private var scoreColor: Color {
        if stock.score >= 80 { return KovaTheme.positive }
        if stock.score >= 60 { return .orange }
        return KovaTheme.negative
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
                        KovaChip(text: ivProfile.uppercased(), color: KovaTheme.blue)
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
            VStack(spacing: 3) {
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

                if stock.isActive == false {
                    Text("inactive")
                        .font(.caption2)
                        .foregroundStyle(KovaTheme.negative)
                }
            }
        }
        .padding(.vertical, 4)
    }
}
