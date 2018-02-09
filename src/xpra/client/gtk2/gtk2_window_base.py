# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010-2018 Antoine Martin <antoine@devloop.org.uk>
# Copyright (C) 2008, 2010 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import gtk
from gtk import gdk
import os.path

from xpra.log import Logger
log = Logger("window")
statelog = Logger("state")
eventslog = Logger("events")
workspacelog = Logger("workspace")
grablog = Logger("grab")
mouselog = Logger("mouse")
draglog = Logger("dragndrop")


from collections import namedtuple
from xpra.client.gtk_base.gtk_client_window_base import GTKClientWindowBase, HAS_X11_BINDINGS
from xpra.gtk_common.gtk_util import WINDOW_NAME_TO_HINT, WINDOW_EVENT_MASK, BUTTON_MASK
from xpra.gtk_common.gobject_util import one_arg_signal
from xpra.util import WORKSPACE_UNSET, WORKSPACE_NAMES, csv, envbool
from xpra.os_util import strtobytes


DRAGNDROP = envbool("XPRA_DRAGNDROP", True)
FORCE_IMMEDIATE_PAINT = envbool("XPRA_FORCE_IMMEDIATE_PAINT", False)


try:
    from xpra.x11.gtk2.gdk_bindings import add_event_receiver       #@UnresolvedImport
except:
    add_event_receiver = None

def wn(w):
    return WORKSPACE_NAMES.get(w, w)

DrawEvent = namedtuple("DrawEvent", "area")


GTK2_OR_TYPE_HINTS = (gdk.WINDOW_TYPE_HINT_DIALOG,
                gdk.WINDOW_TYPE_HINT_MENU, gdk.WINDOW_TYPE_HINT_TOOLBAR,
                #gdk.WINDOW_TYPE_HINT_SPLASHSCREEN,
                #gdk.WINDOW_TYPE_HINT_UTILITY,
                gdk.WINDOW_TYPE_HINT_DOCK,
                #gdk.WINDOW_TYPE_HINT_DESKTOP,
                gdk.WINDOW_TYPE_HINT_DROPDOWN_MENU,
                gdk.WINDOW_TYPE_HINT_POPUP_MENU,
                gdk.WINDOW_TYPE_HINT_TOOLTIP,
                #gdk.WINDOW_TYPE_HINT_NOTIFICATION,
                gdk.WINDOW_TYPE_HINT_COMBO,
                gdk.WINDOW_TYPE_HINT_DND)


"""
GTK2 version of the ClientWindow class
"""
class GTK2WindowBase(GTKClientWindowBase):

    #add GTK focus workaround so we will get focus events
    #even when we grab the keyboard:
    __common_gsignals__ = GTKClientWindowBase.__common_gsignals__
    __common_gsignals__.update({
                                "xpra-focus-out-event"  : one_arg_signal,
                                "xpra-focus-in-event"   : one_arg_signal,
                                })

    WINDOW_EVENT_MASK   = WINDOW_EVENT_MASK
    OR_TYPE_HINTS       = GTK2_OR_TYPE_HINTS
    NAME_TO_HINT        = WINDOW_NAME_TO_HINT
    BUTTON_MASK         = BUTTON_MASK

    WINDOW_STATE_FULLSCREEN = gdk.WINDOW_STATE_FULLSCREEN
    WINDOW_STATE_MAXIMIZED  = gdk.WINDOW_STATE_MAXIMIZED
    WINDOW_STATE_ICONIFIED  = gdk.WINDOW_STATE_ICONIFIED
    WINDOW_STATE_ABOVE      = gdk.WINDOW_STATE_ABOVE
    WINDOW_STATE_BELOW      = gdk.WINDOW_STATE_BELOW
    WINDOW_STATE_STICKY     = gdk.WINDOW_STATE_STICKY


    def do_init_window(self, window_type=gtk.WINDOW_TOPLEVEL):
        gtk.Window.__init__(self, window_type)
        self.recheck_focus_timer = 0
        # tell KDE/oxygen not to intercept clicks
        # see: https://bugs.kde.org/show_bug.cgi?id=274485
        self.set_data("_kde_no_window_grab", 1)
        if DRAGNDROP:
            self.init_dragndrop()

    def destroy(self):
        if self.recheck_focus_timer>0:
            self.source_remove(self.recheck_focus_timer)
            self.recheck_focus_timer = -1
        GTKClientWindowBase.destroy(self)


    def on_realize(self, widget):
        GTKClientWindowBase.on_realize(self, widget)
        #hook up the X11 gdk event notifications so we can get focus-out when grabs are active:
        if add_event_receiver:
            self._focus_latest = None
            grablog("adding event receiver so we can get FocusIn and FocusOut events whilst grabbing the keyboard")
            add_event_receiver(self.get_window(), self)
        #other platforms should bet getting regular focus events instead:
        def focus_in(_window, event):
            grablog("focus-in-event for wid=%s", self._id)
            self.do_xpra_focus_in_event(event)
        def focus_out(_window, event):
            grablog("focus-out-event for wid=%s", self._id)
            self.do_xpra_focus_out_event(event)
        self.connect("focus-in-event", focus_in)
        self.connect("focus-out-event", focus_out)


    ######################################################################
    # drag and drop:
    def init_dragndrop(self):
        targets = [
            ("text/uri-list", 0, 80),
            ]
        flags = gtk.DEST_DEFAULT_MOTION | gtk.DEST_DEFAULT_HIGHLIGHT
        actions = gdk.ACTION_COPY   # | gdk.ACTION_LINK
        self.drag_dest_set(flags, targets, actions)
        self.connect('drag_drop', self.drag_drop_cb)
        self.connect('drag_motion', self.drag_motion_cb)
        self.connect('drag_data_received', self.drag_got_data_cb)

    def drag_drop_cb(self, widget, context, x, y, time):
        draglog("drag_drop_cb%s targets=%s", (widget, context, x, y, time), context.targets)
        if "text/uri-list" not in context.targets:
            draglog("Warning: cannot handle targets:")
            draglog(" %s", csv(context.targets))
            return
        self.drag_get_data(context, "text/uri-list", time)

    def drag_motion_cb(self, wid, context, x, y, time):
        draglog("drag_motion_cb%s", (wid, context, x, y, time))
        context.drag_status(gtk.gdk.ACTION_COPY, time)
        return True #accept this data

    def drag_got_data_cb(self, wid, context, x, y, selection, info, time):
        draglog("drag_got_data_cb%s", (wid, context, x, y, selection, info, time))
        #draglog("%s: %s", type(selection), dir(selection))
        #draglog("%s: %s", type(context), dir(context))
        targets = context.targets
        actions = context.actions
        def xid(w):
            if w:
                return w.xid
            return 0
        dest_window = xid(context.dest_window)
        source_window = xid(context.get_source_window())
        suggested_action = context.get_suggested_action()
        draglog("drag_got_data_cb context: source_window=%#x, dest_window=%#x, suggested_action=%s, actions=%s, targets=%s", source_window, dest_window, suggested_action, actions, targets)
        dtype = selection.get_data_type()
        fmt = selection.get_format()
        l = selection.get_length()
        target = selection.get_target()
        text = selection.get_text()
        uris = selection.get_uris()
        draglog("drag_got_data_cb selection: data type=%s, format=%s, length=%s, target=%s, text=%s, uris=%s", dtype, fmt, l, target, text, uris)
        if not uris:
            return
        filelist = []
        for uri in uris:
            if not uri:
                continue
            if not uri.startswith("file://"):
                draglog.warn("Warning: cannot handle drag-n-drop URI '%s'", uri)
                continue
            filename = strtobytes(uri[len("file://"):].rstrip("\n\r"))
            abspath = os.path.abspath(filename)
            if not os.path.isfile(abspath):
                draglog.warn("Warning: '%s' is not a file", abspath)
                continue
            filelist.append(abspath)
        draglog("drag_got_data_cb: will try to upload: %s", filelist)
        pending = set(filelist)
        #when all the files have been loaded / failed,
        #finish the drag and drop context so the source knows we're done with them:
        def file_done(filename):
            if not pending:
                return
            try:
                pending.remove(filename)
            except:
                pass
            if not pending:
                context.finish(True, False, time)
        for filename in filelist:
            def got_file_info(gfile, result):
                draglog("got_file_info(%s, %s)", gfile, result)
                file_info = gfile.query_info_finish(result)
                basename = gfile.get_basename()
                ctype = file_info.get_content_type()
                size = file_info.get_size()
                draglog("file_info(%s)=%s ctype=%s, size=%s", filename, file_info, ctype, size)
                def got_file_data(gfile, result, user_data=None):
                    data, filesize, entity = gfile.load_contents_finish(result)
                    draglog("got_file_data(%s, %s, %s) entity=%s", gfile, result, user_data, entity)
                    file_done(filename)
                    openit = self._client.remote_open_files
                    draglog.info("sending file %s (%i bytes)", basename, filesize)
                    self._client.send_file(filename, "", data, filesize=filesize, openit=openit)
                gfile.load_contents_async(got_file_data, user_data=(filename, True))
            try:
                import gio
                gfile = gio.File(filename)
                #basename = gf.get_basename()
                gfile.query_info_async("standard::*", got_file_info, flags=gio.FILE_QUERY_INFO_NONE)
            except Exception as e:
                log("file upload for %s:", filename, exc_info=True)
                log.error("Error: cannot upload '%s':", filename)
                log.error(" %s", e)
                del e
                file_done(filename)


    ######################################################################
    # focus:
    def recheck_focus(self):
        self.recheck_focus_timer = 0
        #we receive pairs of FocusOut + FocusIn following a keyboard grab,
        #so we recheck the focus status via this timer to skip unnecessary churn
        focused = self._client._focused
        grablog("recheck_focus() wid=%i, focused=%s, latest=%s", self._id, focused, self._focus_latest)
        hasfocus = focused==self._id
        if not focused:
            #we should never own the grab if we don't have focus
            self.keyboard_ungrab()
            self.pointer_ungrab()
            return
        if hasfocus==self._focus_latest:
            #we're already up to date
            return
        if not self._focus_latest:
            self.keyboard_ungrab()
            self.pointer_ungrab()
            self._client.update_focus(self._id, False)
        else:
            self._client.update_focus(self._id, True)

    def schedule_recheck_focus(self):
        if self.recheck_focus_timer==0:
            self.recheck_focus_timer = self.idle_add(self.recheck_focus)
        return True

    def do_xpra_focus_out_event(self, event):
        grablog("do_xpra_focus_out_event(%s)", event)
        self._focus_latest = False
        return self.schedule_recheck_focus()

    def do_xpra_focus_in_event(self, event):
        grablog("do_xpra_focus_in_event(%s)", event)
        self._focus_latest = True
        return self.schedule_recheck_focus()

    ######################################################################

    def enable_alpha(self):
        screen = self.get_screen()
        rgba = screen.get_rgba_colormap()
        statelog("enable_alpha() rgba colormap=%s", rgba)
        if rgba is None:
            log.error("Error: cannot handle window transparency, no RGBA colormap", exc_info=True)
            return False
        statelog("enable_alpha() using rgba colormap %s for wid %s", rgba, self._id)
        self.set_colormap(rgba)
        return True

    def xget_u32_property(self, target, name):
        try:
            if not HAS_X11_BINDINGS:
                prop = target.property_get(name)
                if not prop or len(prop)!=3 or len(prop[2])!=1:
                    return  None
                log("xget_u32_property(%s, %s)=%s", target, name, prop[2][0])
                return prop[2][0]
        except Exception as e:
            log.error("xget_u32_property error on %s / %s: %s", target, name, e)
        return GTKClientWindowBase.xget_u32_property(self, target, name)

    def is_mapped(self):
        return self.window is not None and self.window.is_visible()

    def get_window_geometry(self):
        gdkwindow = self.get_window()
        x, y = gdkwindow.get_origin()
        _, _, w, h, _ = gdkwindow.get_geometry()
        return x, y, w, h

    def apply_geometry_hints(self, hints):
        self.set_geometry_hints(None, **hints)


    ######################################################################

    def queue_draw(self, x, y, width, height):
        window = self.get_window()
        if not window:
            log.warn("Warning: ignoring draw packet,")
            log.warn(" received for a window which is not realized yet or gone already")
            return
        rect = gdk.Rectangle(x, y, width, height)
        if not FORCE_IMMEDIATE_PAINT:
            window.invalidate_rect(rect, False)
        else:
            #draw directly (bad) to workaround buggy window managers:
            #see: http://xpra.org/trac/ticket/1610
            event = DrawEvent(area=rect)
            self.do_expose_event(event)

    def do_expose_event(self, event):
        #cannot use self
        eventslog("do_expose_event(%s) area=%s", event, event.area)
        backing = self._backing
        if not (self.flags() & gtk.MAPPED) or backing is None:
            return
        w,h = self.window.get_size()
        if w>=32768 or h>=32768:
            log.error("cannot paint on window which is too large: %sx%s !", w, h)
            return
        context = self.window.cairo_create()
        context.rectangle(event.area)
        context.clip()
        self.paint_backing_offset_border(backing, context)
        self.clip_to_backing(backing, context)
        backing.cairo_draw(context)
        self.cairo_paint_border(context, event.area)
        if not self._client.server_ok():
            self.paint_spinner(context, event.area)
