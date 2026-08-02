#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <gbm.h>
#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_drmcommon.h>
#include <va/va_vpp.h>
#include <va/va_enc_h264.h>
#include <drm_fourcc.h>

#define CHK(x) do{VAStatus _s=(x); if(_s!=VA_STATUS_SUCCESS){printf("FAIL %s -> %s\n",#x,vaErrorStr(_s));exit(1);}}while(0)
static double now(){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static int cmpd(const void*a,const void*b){double x=*(double*)a,y=*(double*)b;return x<y?-1:x>y;}

int W=1920,H=1080;
int mbw, mbh;

int main(int argc,char**argv){
  int lowpower = (argc>1 && !strcmp(argv[1],"lp"));
  mbw=(W+15)/16; mbh=(H+15)/16;
  int fd=open("/dev/dri/renderD128",O_RDWR);
  struct gbm_device *gbm=gbm_create_device(fd);
  struct gbm_bo *bo=gbm_bo_create(gbm,W,H,GBM_FORMAT_XRGB8888,GBM_BO_USE_RENDERING|GBM_BO_USE_SCANOUT);
  int dfd=gbm_bo_get_fd(bo);
  VADisplay d=vaGetDisplayDRM(fd); int maj,min; CHK(vaInitialize(d,&maj,&min));
  printf("driver=%s  entrypoint=%s\n",vaQueryVendorString(d), lowpower?"EncSliceLP":"EncSlice");

  VADRMPRIMESurfaceDescriptor desc; memset(&desc,0,sizeof(desc));
  desc.fourcc=VA_FOURCC_BGRX; desc.width=W; desc.height=H;
  desc.num_objects=1; desc.objects[0].fd=dfd;
  desc.objects[0].size=gbm_bo_get_stride_for_plane(bo,0)*H;
  desc.objects[0].drm_format_modifier=gbm_bo_get_modifier(bo);
  desc.num_layers=1; desc.layers[0].drm_format=DRM_FORMAT_XRGB8888;
  desc.layers[0].num_planes=1; desc.layers[0].object_index[0]=0;
  desc.layers[0].offset[0]=gbm_bo_get_offset(bo,0);
  desc.layers[0].pitch[0]=gbm_bo_get_stride_for_plane(bo,0);
  VASurfaceAttrib ia[2]={
    {VASurfaceAttribMemoryType,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2}}},
    {VASurfaceAttribExternalBufferDescriptor,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypePointer,{.p=&desc}}}};
  VASurfaceID rgbsurf; CHK(vaCreateSurfaces(d,VA_RT_FORMAT_RGB32,W,H,&rgbsurf,1,ia,2));

  VASurfaceAttrib na={VASurfaceAttribPixelFormat,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_FOURCC_NV12}}};
  VASurfaceID pool[6]; CHK(vaCreateSurfaces(d,VA_RT_FORMAT_YUV420,W,H,pool,6,&na,1));
  VASurfaceID cur=pool[0], ref0=pool[1], ref1=pool[2];

  VAConfigID vcfg; VAContextID vctx;
  CHK(vaCreateConfig(d,VAProfileNone,VAEntrypointVideoProc,NULL,0,&vcfg));
  CHK(vaCreateContext(d,vcfg,W,H,VA_PROGRESSIVE,pool,6,&vctx));

  VAEntrypoint ep = lowpower?VAEntrypointEncSliceLP:VAEntrypointEncSlice;
  VAConfigAttrib ea[2]={{VAConfigAttribRTFormat,VA_RT_FORMAT_YUV420},{VAConfigAttribRateControl, lowpower?VA_RC_CQP:VA_RC_CBR}};
  VAConfigID ecfg; VAContextID ectx;
  CHK(vaCreateConfig(d,VAProfileH264ConstrainedBaseline,ep,ea,2,&ecfg));
  CHK(vaCreateContext(d,ecfg,W,H,VA_PROGRESSIVE,pool,6,&ectx));
  VABufferID coded; CHK(vaCreateBuffer(d,ectx,VAEncCodedBufferType,W*H*3/2,1,NULL,&coded));

  VAEncSequenceParameterBufferH264 sps; memset(&sps,0,sizeof(sps));
  sps.seq_parameter_set_id=0; sps.level_idc=42;
  sps.intra_period=0; sps.intra_idr_period=0; sps.ip_period=1;   // IDR only on demand, all-P
  sps.bits_per_second=10000000;
  sps.max_num_ref_frames=1;
  sps.picture_width_in_mbs=mbw; sps.picture_height_in_mbs=mbh;
  sps.seq_fields.bits.frame_mbs_only_flag=1;
  sps.seq_fields.bits.chroma_format_idc=1;
  sps.seq_fields.bits.log2_max_frame_num_minus4=4;
  sps.seq_fields.bits.pic_order_cnt_type=2;
  sps.seq_fields.bits.direct_8x8_inference_flag=1;
  sps.frame_cropping_flag = (H%16)?1:0;
  sps.frame_crop_bottom_offset = (H%16)? (16-(H%16))/2 : 0;
  sps.time_scale=120; sps.num_units_in_tick=1; sps.vui_parameters_present_flag=0;

  VAEncPictureParameterBufferH264 pps; memset(&pps,0,sizeof(pps));
  VAEncSliceParameterBufferH264 sl;
  VAEncMiscParameterBuffer *mp;

  double lat[600]; int nl=0;
  double t_vpp[600];
  int NF=300;
  unsigned long frame_num=0; int poc=0;
  for(int f=0; f<NF; f++){
    int idr = (f==0);
    cur = pool[f%3]; ref0 = pool[(f+2)%3];

    // --- VPP: dmabuf BGRX -> NV12 ---
    double tv0=now();
    VAProcPipelineParameterBuffer p; memset(&p,0,sizeof(p));
    VARectangle r={0,0,W,H};
    p.surface=rgbsurf; p.surface_region=&r; p.output_region=&r;
    p.surface_color_standard=VAProcColorStandardNone;
    p.output_color_standard=VAProcColorStandardBT709;
    p.filter_flags=VA_FRAME_PICTURE|VA_FILTER_SCALING_FAST;
    VABufferID pb; CHK(vaCreateBuffer(d,vctx,VAProcPipelineParameterBufferType,sizeof(p),1,&p,&pb));
    CHK(vaBeginPicture(d,vctx,cur));
    CHK(vaRenderPicture(d,vctx,&pb,1));
    CHK(vaEndPicture(d,vctx));
    vaDestroyBuffer(d,pb);
    double tv1=now();

    // --- ENCODE ---
    double t0=now();
    memset(&pps,0,sizeof(pps));
    pps.CurrPic.picture_id=cur; pps.CurrPic.TopFieldOrderCnt=poc; pps.CurrPic.BottomFieldOrderCnt=poc;
    pps.CurrPic.frame_idx=frame_num; pps.CurrPic.flags=0;
    for(int i=0;i<16;i++){pps.ReferenceFrames[i].picture_id=VA_INVALID_ID;pps.ReferenceFrames[i].flags=VA_PICTURE_H264_INVALID;}
    if(!idr){ pps.ReferenceFrames[0].picture_id=ref0; pps.ReferenceFrames[0].frame_idx=frame_num-1;
              pps.ReferenceFrames[0].flags=VA_PICTURE_H264_SHORT_TERM_REFERENCE; }
    pps.coded_buf=coded;
    pps.pic_parameter_set_id=0; pps.seq_parameter_set_id=0;
    pps.frame_num=frame_num;
    pps.pic_init_qp=26;
    pps.num_ref_idx_l0_active_minus1=0;
    pps.pic_fields.bits.idr_pic_flag=idr;
    pps.pic_fields.bits.reference_pic_flag=1;
    pps.pic_fields.bits.entropy_coding_mode_flag=0; // CAVLC (baseline)
    pps.pic_fields.bits.deblocking_filter_control_present_flag=1;
    pps.last_picture=0;

    VABufferID sb,pb2,slb, rcb=VA_INVALID_ID, hrdb=VA_INVALID_ID, frb=VA_INVALID_ID;
    CHK(vaCreateBuffer(d,ectx,VAEncSequenceParameterBufferType,sizeof(sps),1,&sps,&sb));
    CHK(vaCreateBuffer(d,ectx,VAEncPictureParameterBufferType,sizeof(pps),1,&pps,&pb2));
    memset(&sl,0,sizeof(sl));
    sl.macroblock_address=0; sl.num_macroblocks=mbw*mbh;
    sl.slice_type = idr?2:0;
    sl.idr_pic_id=0; sl.pic_order_cnt_lsb=poc;
    sl.num_ref_idx_active_override_flag=1; sl.num_ref_idx_l0_active_minus1=0;
    for(int i=0;i<32;i++){sl.RefPicList0[i].picture_id=VA_INVALID_ID;sl.RefPicList0[i].flags=VA_PICTURE_H264_INVALID;
                          sl.RefPicList1[i].picture_id=VA_INVALID_ID;sl.RefPicList1[i].flags=VA_PICTURE_H264_INVALID;}
    if(!idr){sl.RefPicList0[0].picture_id=ref0;sl.RefPicList0[0].frame_idx=frame_num-1;sl.RefPicList0[0].flags=VA_PICTURE_H264_SHORT_TERM_REFERENCE;}
    sl.slice_alpha_c0_offset_div2=0; sl.slice_beta_offset_div2=0;
    CHK(vaCreateBuffer(d,ectx,VAEncSliceParameterBufferType,sizeof(sl),1,&sl,&slb));

    if(!lowpower){
      // CBR + tiny HRD window (1 frame)
      CHK(vaCreateBuffer(d,ectx,VAEncMiscParameterBufferType,sizeof(VAEncMiscParameterBuffer)+sizeof(VAEncMiscParameterRateControl),1,NULL,&rcb));
      vaMapBuffer(d,rcb,(void**)&mp); mp->type=VAEncMiscParameterTypeRateControl;
      VAEncMiscParameterRateControl*rc=(VAEncMiscParameterRateControl*)mp->data;
      memset(rc,0,sizeof(*rc)); rc->bits_per_second=10000000; rc->target_percentage=100;
      rc->window_size=17; rc->initial_qp=26; rc->min_qp=10; rc->max_qp=40;
      rc->rc_flags.bits.disable_frame_skip=1;
      vaUnmapBuffer(d,rcb);
      CHK(vaCreateBuffer(d,ectx,VAEncMiscParameterBufferType,sizeof(VAEncMiscParameterBuffer)+sizeof(VAEncMiscParameterHRD),1,NULL,&hrdb));
      vaMapBuffer(d,hrdb,(void**)&mp); mp->type=VAEncMiscParameterTypeHRD;
      VAEncMiscParameterHRD*hrd=(VAEncMiscParameterHRD*)mp->data;
      hrd->buffer_size=10000000/30; hrd->initial_buffer_fullness=hrd->buffer_size/2;
      vaUnmapBuffer(d,hrdb);
      CHK(vaCreateBuffer(d,ectx,VAEncMiscParameterBufferType,sizeof(VAEncMiscParameterBuffer)+sizeof(VAEncMiscParameterFrameRate),1,NULL,&frb));
      vaMapBuffer(d,frb,(void**)&mp); mp->type=VAEncMiscParameterTypeFrameRate;
      VAEncMiscParameterFrameRate*fr=(VAEncMiscParameterFrameRate*)mp->data;
      memset(fr,0,sizeof(*fr)); fr->framerate=60;
      vaUnmapBuffer(d,frb);
    }

    CHK(vaBeginPicture(d,ectx,cur));
    VABufferID bufs[8]; int nb=0;
    bufs[nb++]=sb; if(rcb!=VA_INVALID_ID)bufs[nb++]=rcb;
    if(hrdb!=VA_INVALID_ID)bufs[nb++]=hrdb; if(frb!=VA_INVALID_ID)bufs[nb++]=frb;
    bufs[nb++]=pb2; bufs[nb++]=slb;
    CHK(vaRenderPicture(d,ectx,bufs,nb));
    CHK(vaEndPicture(d,ectx));
    CHK(vaSyncSurface(d,cur));
    VACodedBufferSegment *seg;
    CHK(vaMapBuffer(d,coded,(void**)&seg));
    unsigned sz=seg->size;
    vaUnmapBuffer(d,coded);
    double t1=now();
    for(int i=0;i<nb;i++) vaDestroyBuffer(d,bufs[i]);
    if(f>=30){ lat[nl]=(t1-t0)*1000.0; t_vpp[nl]=(tv1-tv0)*1000.0; nl++; }
    if(f==1) printf("  (frame1 size=%u bytes)\n",sz);
    frame_num=(frame_num+1)&0xffff; poc+=2;
  }
  qsort(lat,nl,sizeof(double),cmpd);
  double sum=0; for(int i=0;i<nl;i++)sum+=lat[i];
  printf("ENCODE submit->coded-bits-in-hand (n=%d): min=%.2f p50=%.2f p90=%.2f p99=%.2f max=%.2f mean=%.2f ms\n",
    nl,lat[0],lat[nl/2],lat[nl*9/10],lat[nl*99/100],lat[nl-1],sum/nl);
  return 0;
}
