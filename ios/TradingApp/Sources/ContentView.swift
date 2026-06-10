import SwiftUI

// ── Macro calendar annual update reminder ────────────────────────────────────
// Shows Jan 1–7 each year until the user confirms they've updated
// FOMC / CPI / Jobs date sets in macro_calendar.py for the new year.
private struct MacroCalendarReminderBanner: View {
    @AppStorage("macroCalendarConfirmedYear") private var confirmedYear: Int = 0

    private var shouldShow: Bool {
        let cal  = Calendar.current
        let now  = Date()
        let year = cal.component(.year,  from: now)
        let month = cal.component(.month, from: now)
        let day   = cal.component(.day,   from: now)
        return month == 1 && day <= 7 && confirmedYear != year
    }

    var body: some View {
        if shouldShow {
            HStack(spacing: 10) {
                Image(systemName: "calendar.badge.exclamationmark")
                    .foregroundStyle(.white)
                    .font(.system(size: 18))

                VStack(alignment: .leading, spacing: 2) {
                    Text("Update Macro Calendar Dates")
                        .font(.subheadline).fontWeight(.bold).foregroundStyle(.white)
                    Text("Add FOMC, CPI & Jobs dates for \(Calendar.current.component(.year, from: Date())) in macro_calendar.py")
                        .font(.caption).foregroundStyle(.white.opacity(0.9))
                }

                Spacer()

                Button {
                    confirmedYear = Calendar.current.component(.year, from: Date())
                } label: {
                    Text("Done ✓")
                        .font(.caption).fontWeight(.semibold)
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(.white.opacity(0.25))
                        .clipShape(Capsule())
                        .foregroundStyle(.white)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .background(Color.orange)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .transition(.move(edge: .top).combined(with: .opacity))
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────

struct ContentView: View {
    var body: some View {
        VStack(spacing: 0) {
            MacroCalendarReminderBanner()
            TabView {
                DashboardView()
                    .tabItem {
                        Label("Dashboard", systemImage: "house.fill")
                    }

                OrdersView()
                    .tabItem {
                        Label("Orders", systemImage: "list.bullet.rectangle")
                    }

                NewsView()
                    .tabItem {
                        Label("News", systemImage: "newspaper.fill")
                    }

                AIView()
                    .tabItem {
                        Label("AI Agent", systemImage: "brain")
                    }

                InsightsView()
                    .tabItem {
                        Label("Insights", systemImage: "lightbulb.fill")
                    }

                WheelView()
                    .tabItem {
                        Label("Wheel", systemImage: "arrow.2.circlepath")
                    }

                PureAIView()
                    .tabItem {
                        Label("Pure AI", systemImage: "brain.head.profile")
                    }

                SettingsView()
                    .tabItem {
                        Label("Settings", systemImage: "gearshape.fill")
                    }
            }
            .tint(KovaTheme.purple)
        }
    }
}
