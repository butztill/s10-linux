// SPDX-License-Identifier: GPL-2.0-only
#include "s10_bayer_resize.h"
#include <cassert>
#include <iostream>
using libcamera::S10BayerResize;
int main()
{
 const unsigned w=400,h=300,stride=w*2+32;
 std::vector<uint8_t> raw(stride*h,0);
 auto fill=[&](bool ramp) {
  for(unsigned y=0;y<h;++y) for(unsigned x=0;x<w;++x) {
   unsigned v=ramp?x+y*2:100+200*(x&1)+400*(y&1);
   raw[y*stride+x*2]=v&255; raw[y*stride+x*2+1]=v>>8;
  }
 };
 auto get=[](const uint8_t*p,unsigned n){return unsigned(p[n*2]+256*p[n*2+1]);};
 S10BayerResize s;
 fill(false);
 assert(s.configure(w,h,stride,80,60,4,3));
 assert(s.process(raw.data(),raw.size()));
 for(unsigned y=0;y<60;++y) for(unsigned x=0;x<80;++x)
  assert(get(s.data(),y*80+x)==100+200*(x&1)+400*(y&1));
 fill(true); assert(s.process(raw.data(),raw.size()));
 // Both ends of the full scene survive resizing, unlike a centre crop.
 assert(get(s.data(),0)<20 && get(s.data(),80*60-1)>970);
 assert(s.configure(w,h,stride,160,90,16,9));
 assert(s.process(raw.data(),raw.size()));
 // Aspect matching crops vertically but retains essentially all scene width.
 assert(get(s.data(),159)-get(s.data(),0)>390);
 assert(!s.process(raw.data(),30));
 assert(!s.configure(w,h,10,80,60,4,3));
 assert(!s.configure(w,h,stride,81,60,4,3));
 std::cout<<"Bayer phase, full field of view, aspect crop and padded/truncated input checks passed\n";
}
