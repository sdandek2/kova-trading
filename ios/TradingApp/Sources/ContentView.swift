import SwiftUI

struct ContentView: View {
    @State private var selectedTab = 0
    @Namespace private var tabNS
    @AppStorage("macroCalendarConfirmedYear") private var confirmedYear: Int = 0

    var body: some View {
        ZStack(alignment: .bottom) {
            // ── Page content ──────────────────────────────────────────────
            TabView(selection: $selectedTab) {
                DashboardView()
                    .tag(0)
                    .toolbar(.hidden, for: .tabBar)

                OrdersView()
                    .tag(1)
                    .toolbar(.hidden, for: .tabBar)

                LabsView()
                    .tag(2)
                    .toolbar(.hidden, for: .tabBar)

                PureAIView()
                    .tag(3)
                    .toolbar(.hidden, for: .tabBar)

                WheelView()
                    .tag(4)
                    .toolbar(.hidden, for: .tabBar)

                MoreView()
                    .tag(5)
                    .toolbar(.hidden, for: .tabBar)
            }
            // Reserve space so content doesn't hide behind custom tab bar
            .safeAreaInset(edge: .bottom) { Color.clear.frame(height: 90) }

            // ── Floating tab bar + safe-area fill ────────────────────────
            VStack(spacing: 0) {
                // Macro calendar nudge (Jan 1–7 only)
                if shouldShowMacroBanner {
                    MacroBanner { confirmedYear = Calendar.current.component(.year, from: Date()) }
                        .padding(.horizontal, 18)
                        .padding(.bottom, 6)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
                FloatingTabBar(selected: $selectedTab, namespace: tabNS)
            }
            // Fill the gap between the capsule and the screen bottom edge
            .background(
                Rectangle()
                    .fill(.ultraThinMaterial)
                    .ignoresSafeArea(edges: .bottom)
            )
        }
        .ignoresSafeArea(edges: .bottom)
        .tint(LakshmiTheme.gold)
        .animation(LakshmiTheme.springSnappy, value: selectedTab)
    }

    private var shouldShowMacroBanner: Bool {
        let cal = Calendar.current
        let now = Date()
        let month = cal.component(.month, from: now)
        let day   = cal.component(.day,   from: now)
        let year  = cal.component(.year,  from: now)
        return month == 1 && day <= 7 && confirmedYear != year
    }
}

// MARK: - Macro calendar banner (compact version for floating area)

private struct MacroBanner: View {
    let onDismiss: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "calendar.badge.exclamationmark")
                .foregroundStyle(.white)
            VStack(alignment: .leading, spacing: 1) {
                Text("Update Macro Calendar").font(.caption.weight(.bold)).foregroundStyle(.white)
                Text("Add FOMC/CPI/Jobs dates for \(Calendar.current.component(.year, from: Date()))")
                    .font(.caption2).foregroundStyle(.white.opacity(0.85))
            }
            Spacer()
            Button(action: onDismiss) {
                Text("Done ✓")
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .background(.white.opacity(0.25))
                    .clipShape(Capsule())
                    .foregroundStyle(.white)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(Color.orange)
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }
}
