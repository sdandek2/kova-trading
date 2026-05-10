import SwiftUI

struct ContentView: View {
    var body: some View {
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
        }
    }
}
