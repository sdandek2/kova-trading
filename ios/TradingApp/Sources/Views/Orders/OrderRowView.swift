import SwiftUI

struct OrderRowView: View {
    let order: Order

    private var isBuy:  Bool { order.side == "buy" }
    private var sideColor: Color { isBuy ? KovaTheme.positive : KovaTheme.negative }
    private var sideLabel: String { order.side.uppercased() }

    var body: some View {
        HStack(spacing: 12) {
            // ── Side icon ─────────────────────────────────────────────────
            ZStack {
                RoundedRectangle(cornerRadius: 10)
                    .fill(sideColor.opacity(0.12))
                    .frame(width: 40, height: 40)
                Image(systemName: isBuy ? "arrow.down.circle.fill" : "arrow.up.circle.fill")
                    .foregroundStyle(sideColor)
                    .font(.system(size: 18))
            }

            // ── Symbol + qty ──────────────────────────────────────────────
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(order.symbol)
                        .font(.subheadline.weight(.bold))
                    KovaChip(text: sideLabel, color: sideColor)
                }
                Text("\(Int(order.qty)) shares")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            // ── Price + status ────────────────────────────────────────────
            VStack(alignment: .trailing, spacing: 3) {
                if let price = order.filledAvgPrice {
                    Text(String(format: "$%.2f", price))
                        .font(.subheadline.weight(.semibold))
                }
                Text(order.status.replacingOccurrences(of: "_", with: " ").capitalized)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(statusColor(order.status))
                Text(order.createdAt, style: .relative)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 6)
        .opacity(["filled", "new", "accepted", "partially_filled", "pending_new"].contains(order.status) ? 1.0 : 0.55)
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "filled":                          return KovaTheme.positive
        case "canceled", "rejected", "expired": return KovaTheme.negative
        default:                                return .secondary
        }
    }
}
