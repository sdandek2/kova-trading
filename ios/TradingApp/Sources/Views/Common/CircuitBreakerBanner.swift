import SwiftUI

/// A persistent red banner shown at the top of the AI Agent tab
/// when the bot's daily loss limit has been hit and new buys are blocked.
/// Disappears automatically once the portfolio recovers above the limit.
struct CircuitBreakerBanner: View {
    let dayPlPercent: Double
    let limitPct: Double

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.white)
                .font(.system(size: 16))

            VStack(alignment: .leading, spacing: 2) {
                Text("Circuit Breaker Active")
                    .font(.subheadline).fontWeight(.bold).foregroundStyle(.white)
                Text("Down \(abs(dayPlPercent), specifier: "%.1f")% today (limit \(limitPct, specifier: "%.0f")%). New buys blocked — exits still open.")
                    .font(.caption).foregroundStyle(.white.opacity(0.9))
            }

            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Color.red)
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }
}
