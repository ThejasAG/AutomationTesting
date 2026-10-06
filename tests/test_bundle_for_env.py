from automation.scenarios.cross_app_flows import bundle_for_env

def test_translates_both_directions_and_leaves_strangers_alone():
    assert bundle_for_env("org.vyapy.sarls.vyaconsumer", "staging") == "org.vyapy.sarls.vyaconsumerstaging"
    assert bundle_for_env("org.vyapy.sarls.vyabusinessipadstaging", "prod") == "org.vyapy.sarls.vyabusinessipad"
    assert bundle_for_env("org.vyapy.sarls.vyaconsumer", "prod") == "org.vyapy.sarls.vyaconsumer"
    assert bundle_for_env("com.apple.Preferences", "staging") == "com.apple.Preferences"  # not ours
