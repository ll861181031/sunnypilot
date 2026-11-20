"""Independent, monotonic reset requests for the two navigation histories."""


NAV_STATE_KEYS = {'map': 'state_map_features_q', 'navigation': 'state_navigation_features_q'}
NAV_QUEUE_KEYS = {'map': 'state_map_q', 'navigation': 'state_nav_q'}


class NavigationResetMixin:
  nav_reset_supported = True

  def request_nav_reset(self, *, map_epoch=None, navigation_epoch=None):
    requested = dict(getattr(self, '_nav_reset_requested', {}))
    for channel, epoch in (('map', map_epoch), ('navigation', navigation_epoch)):
      if epoch is None:
        continue
      if type(epoch) is not int or not 0 <= epoch < 1 << 64 or epoch < requested.get(channel, 0):
        raise ValueError(f'Invalid navigation reset epoch for {channel}')
      requested[channel] = epoch
    self._nav_reset_requested = requested

  @property
  def nav_reset_applied(self):
    return dict(getattr(self, '_nav_reset_applied', {}))

  def pending_nav_resets(self):
    applied = self.nav_reset_applied
    return {channel: epoch for channel, epoch in getattr(self, '_nav_reset_requested', {}).items()
            if applied.get(channel) != epoch}

  def _commit_nav_reset(self, pending):
    self._nav_reset_applied = self.nav_reset_applied | pending
