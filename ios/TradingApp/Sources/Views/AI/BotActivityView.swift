import SwiftUI

struct BotActivityView: View {
    @State private var events: [BotActivity] = []
    @State private var isLoading = true

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // ── Header ──
            HStack(spacing: 10) {
                ZStack {
                    Circle().fill(Color.blue.opacity(0.12)).frame(width: 36, height: 36)
                    Image(systemName: "list.bullet.clipboard")
                        .foregroundStyle(.blue).font(.system(size: 16))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Bot Activity Log")
                        .font(.headline).foregroundStyle(.primary)
                    Text(isLoading ? "Loading..." : "\(events.count) recent events")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if isLoading { ProgressView().scaleEffect(0.8) }
            }
            .padding()

            if !events.isEmpty {
                Divider().padding(.horizontal)

                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(events) { event in
                            BotActivityRow(event: event)
                            Divider().padding(.leading, 52)
                        }
                    }
                    .padding(.bottom, 8)
                }
                .frame(maxHeight: 320)
            }
        }
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        isLoading = true
        events = (try? await APIService.shared.getBotActivity(limit: 50)) ?? []
        isLoading = false
    }
}

struct BotActivityRow: View {
    let event: BotActivity

    var iconColor: Color {
        switch event.event_type {
        case "approved":        return .green
        case "entry_rejected":  return .orange
        case "earnings_block":  return .yellow
        case "circuit_breaker": return .red
        case "trailing_stop":   return .purple
        case "scale_out":       return .blue
        default:                return .secondary
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: event.eventIcon)
                .foregroundStyle(iconColor)
                .font(.system(size: 16))
                .frame(width: 28, height: 28)
                .background(iconColor.opacity(0.12))
                .clipShape(Circle())

            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    if let sym = event.symbol {
                        Text(sym).font(.caption).fontWeight(.semibold)
                    }
                    Text(event.event_type.replacingOccurrences(of: "_", with: " ").capitalized)
                        .font(.caption2).foregroundStyle(.secondary)
                    Spacer()
                    if let ts = event.timestamp {
                        Text(relativeTime(ts))
                            .font(.caption2).foregroundStyle(.tertiary)
                    }
                }
                Text(event.message)
                    .font(.caption)
                    .foregroundStyle(.primary)
                    .lineLimit(2)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    private func relativeTime(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = formatter.date(from: iso) else { return "" }
        let mins = Int(-date.timeIntervalSinceNow / 60)
        if mins < 1 { return "now" }
        if mins < 60 { return "\(mins)m ago" }
        return "\(mins / 60)h ago"
    }
}
