def test_commerce_common_is_importable():
    from commerce_common.execution import BaseToolExecutor
    from commerce_common.fencing import Fence
    from commerce_common.presentation import PresentationExtension
    assert Fence and BaseToolExecutor and PresentationExtension


def test_role_packages_exist():
    import atworks_agent
    import atworks_agent_runtime
    import atworks_host
    assert atworks_agent and atworks_agent_runtime and atworks_host
