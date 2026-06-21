import SwiftUI

struct PositionRowView: View {
    let position: Position
    @Environment(\.colorScheme) private var colorScheme

    private var isShort: Bool { position.side == "short" }
    private var plColor: Color { position.unrealizedPl >= 0 ? LakshmiTheme.positive : LakshmiTheme.negative }

    var body: some View {
        HStack(spacing: 12) {
            // ── Symbol icon ───────────────────────────────────────────────
            ZStack {
                RoundedRectangle(cornerRadius: 10)
                    .fill(isShort ? LakshmiTheme.negative.opacity(0.12) : LakshmiTheme.blue.opacity(0.12))
                    .frame(width: 42, height: 42)
                Text(String(position.symbol.prefix(2)))
                    .font(.system(size: 13, weight: .bold, design: .rounded))
                    .foregroundStyle(isShort ? LakshmiTheme.negative : LakshmiTheme.blue)
            }

            // ── Symbol + details ──────────────────────────────────────────
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(position.symbol)
                        .font(.subheadline.weight(.bold))
                    if isShort {
                        LakshmiChip(text: "SHORT", color: LakshmiTheme.negative)
                    }
                }
                Text(String(format: "%d shares · avg $%.2f",
                             Int(position.qty), position.avgEntryPrice))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            // ── Price + P&L ───────────────────────────────────────────────
            VStack(alignment: .trailing, spacing: 4) {
                Text(String(format: "$%.2f", position.currentPrice))
                    .font(.subheadline.weight(.semibold))

                HStack(spacing: 3) {
                    Text(String(format: "%@$%.2f",
                                position.unrealizedPl >= 0 ? "+" : "",
                                position.unrealizedPl))
                    Text(String(format: "(%.1f%%)", position.unrealizedPlPercent))
                        .opacity(0.7)
                }
                .font(.caption.weight(.medium))
                .foregroundStyle(plColor)
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(plColor.opacity(0.10))
                .clipShape(Capsule())
            }
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 14)
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm))
    }
}
