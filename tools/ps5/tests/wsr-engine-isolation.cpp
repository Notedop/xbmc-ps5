/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "in_wsr/wsr_player.h"

#include <cassert>

extern "C"
{
decltype(::WSRPlayerSetUp) in_wsr_track_WSRPlayerSetUp;
decltype(::WSRPlayerSetUp) in_wsr_0_WSRPlayerSetUp;
decltype(::WSRPlayerSetUp) in_wsr_1_WSRPlayerSetUp;
}

int main()
{
  WSRPlayerApi* track = in_wsr_track_WSRPlayerSetUp();
  WSRPlayerApi* first = in_wsr_0_WSRPlayerSetUp();
  WSRPlayerApi* second = in_wsr_1_WSRPlayerSetUp();
  assert(track && first && second);
  assert(track != first && track != second && first != second);

  track->p_Set_ChannelMuting(1);
  first->p_Set_ChannelMuting(2);
  second->p_Set_ChannelMuting(4);
  assert(track->p_Get_ChannelMuting() == 1);
  assert(first->p_Get_ChannelMuting() == 2);
  assert(second->p_Get_ChannelMuting() == 4);

  first->p_Set_ChannelMuting(0);
  assert(track->p_Get_ChannelMuting() == 1);
  assert(first->p_Get_ChannelMuting() == 0);
  assert(second->p_Get_ChannelMuting() == 4);
}
