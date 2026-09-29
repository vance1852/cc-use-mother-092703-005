"""绿色再制造与再认证追踪服务。

覆盖回收设备登记、拆解谱系、部件检测/修复、版本化工艺规范与复用规则、
再认证决定证据快照、校准失效影响定位，以及材料最终去向查询。
"""

from .service import RemanufactureService

__all__ = ["RemanufactureService"]
