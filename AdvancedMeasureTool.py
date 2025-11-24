from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from qgis.core import (
    QgsProject,
    QgsGeometry,
    QgsFeature,
    QgsVectorLayer,
    QgsField,
    QgsWkbTypes,
    QgsDistanceArea,
    QgsLineSymbol,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    Qgis
)

from qgis.gui import QgsMapTool, QgsRubberBand, QgsMapCanvas

import os
from datetime import datetime

class AdvancedMeasureTool:

    def __init__(self, iface):
        self.iface = iface
        self.canvas: QgsMapCanvas = iface.mapCanvas()
        self.toolbar = None
        self.measure_action = None
        self.tool = None
        self.icon_path = os.path.join(os.path.dirname(__file__), 'icon.svg')

    def initGui(self):
        self.toolbar = self.iface.addToolBar("Advanced Measure Tool")
        self.toolbar.setObjectName("AdvancedMeasureToolToolbar")

        self.measure_action = QAction(QIcon(self.icon_path), "Advanced Measure Tool", self.iface.mainWindow())
        self.measure_action.setCheckable(True)
        self.measure_action.triggered.connect(self.toggle_tool)
        self.toolbar.addAction(self.measure_action)

    def unload(self):
        if self.toolbar:
            try:
                self.iface.mainWindow().removeToolBar(self.toolbar)
                del self.toolbar
            except Exception:
                try:
                    self.iface.removeToolBar(self.toolbar)
                except Exception:
                    pass
            self.toolbar = None
        if self.tool and self.canvas.mapTool() == self.tool:
            self.canvas.unsetMapTool(self.tool)
            self.tool = None

    def toggle_tool(self):
        if self.tool is None:
            self.tool = self._MeasureMapTool(self.iface, self.canvas, self.measure_action)
        if self.canvas.mapTool() == self.tool:
            self.canvas.unsetMapTool(self.tool)
            self.measure_action.setChecked(False)
        else:
            self.canvas.setMapTool(self.tool)
            self.measure_action.setChecked(True)


    class _MeasureMapTool(QgsMapTool):
        """Inner map tool implementing live measurement behavior with undo (right-click),
        cancel (Esc), Line_ID counter, and final layer creation with Start/Stop as WKT POINT fields."""

        def __init__(self, iface, canvas, action):
            super().__init__(canvas)
            self.iface = iface
            self.canvas = canvas
            self.action = action

            # data storage (IN MEMORY ONLY)
            # each row: { 'Line_ID': int, 'P1x', 'P1y', 'P2x', 'P2y', 'length_m', 'length_nm', 'cum_length_m', 'cum_length_nm' }
            self.table_rows = []
            self.n = 0 # index of current segment (starts at 0 for first segment)
            self.var_cum_length_m = 0.0

            # measurement state
            self.last_point = None
            self.click_count = 0
            self.is_measuring = False

            # rubber bands for drawing persistent segments and temp segment
            self.temp_rb = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
            self.temp_rb.setWidth(2)
            self.temp_rb.setColor(Qt.red)
            self.main_rb = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
            self.main_rb.setWidth(3)
            self.main_rb.setColor(Qt.red)

            # distance calculator set to project CRS
            self.da = QgsDistanceArea()
            self.da.setSourceCrs(canvas.mapSettings().destinationCrs(),
                                 QgsProject.instance().transformContext())
            self.da.setEllipsoid(QgsProject.instance().ellipsoid() or 'WGS84')

            # Line_ID counter for points (counts points starting from 1)
            self.point_id_counter = 0

        ###################################################################
        # LEFT CLICK — build table in memory only
        ###################################################################
        def canvasPressEvent(self, event):
            if event.button() != Qt.LeftButton:
                return

            pt = self.toMapCoordinates(event.pos())
            self.click_count += 1

            if self.click_count == 1:
                self.start_new_measurement()
                self.last_point = pt
                self.point_id_counter += 1
                
                # First row only stores P1
                self.table_rows.append({
                    'Line_ID': self.point_id_counter,
                    'P1x': round(pt.x(), 6),
                    'P1y': round(pt.y(), 6),
                    'P2x': None,
                    'P2y': None,
                    'length_m': None,
                    'length_nm': None,
                    'cum_length_m': None,
                    'cum_length_nm': None
                })

            else:
                # fill P2 of previous row
                self.table_rows[self.n]['P2x'] = round(pt.x(), 6)
                self.table_rows[self.n]['P2y'] = round(pt.y(), 6)

                # compute segment
                self.calculate_segment(self.n)

                # draw segment
                self.main_rb.addPoint(self.last_point)
                self.main_rb.addPoint(pt)

                # next row begins at this point
                self.last_point = pt
                self.point_id_counter += 1
                self.n += 1

                # new row for next segment
                self.table_rows.append({
                    'Line_ID': self.point_id_counter,
                    'P1x': round(pt.x(), 6),
                    'P1y': round(pt.y(), 6),
                    'P2x': None,
                    'P2y': None,
                    'length_m': None,
                    'length_nm': None,
                    'cum_length_m': None,
                    'cum_length_nm': None
                })


        ###################################################################
        # MOVE — show temporary preview
        ###################################################################
        def canvasMoveEvent(self, event):
            if not self.is_measuring or self.last_point is None:
                return

            cur_pt = self.toMapCoordinates(event.pos())

            # update temp rubber band
            self.temp_rb.reset(QgsWkbTypes.LineGeometry)
            self.temp_rb.addPoint(self.last_point)
            self.temp_rb.addPoint(cur_pt)

            # compute temporary length
            geom = QgsGeometry.fromPolylineXY([self.last_point, cur_pt])
            length_m = self.da.measureLength(geom)
            length_nm = length_m / 1852.0
            cum_m = self.var_cum_length_m + length_m
            cum_nm = cum_m / 1852.0

            # Clear old messages
            self.iface.messageBar().clearWidgets()

            self.iface.messageBar().pushMessage(
                "Measure",
                f"Segment: {length_m:.1f} m ({length_nm:.2f} nm) | Total: {cum_m:.1f} m ({cum_nm:.2f} nm)",
                level=Qgis.Info,
                duration=5
            )


        ###################################################################
        # DOUBLE CLICK — create final layer (table + geometry)
        ###################################################################
        def canvasDoubleClickEvent(self, event):
            if not self.is_measuring:
                return

            # remove temporary rubber band
            self.temp_rb.reset(QgsWkbTypes.LineGeometry)

            # finalize data
            self.finish_measurement()


        ###################################################################
        # CREATE FINAL LAYER ON DOUBLE CLICK
        ###################################################################
        def finish_measurement(self):
            self.is_measuring = False

            # filter out incomplete last row
            final_rows = [r for r in self.table_rows if r['P2x'] is not None]
            if len(final_rows) == 0:
                return

            # create memory layer
            crs = self.canvas.mapSettings().destinationCrs().authid()
            layer = QgsVectorLayer(f"LineString?crs={crs}", f"Measurement_{datetime.now().strftime('%Y%m%d_%H%M%S')}", "memory")
            pr = layer.dataProvider()

            # add fields
            pr.addAttributes([
                QgsField("Line_ID", QVariant.Int),
                QgsField("Start", QVariant.String),
                QgsField("Stop", QVariant.String),
                QgsField("length_m", QVariant.Double),
                QgsField("length_nm", QVariant.Double),
                QgsField("cum_length_m", QVariant.Double),
                QgsField("cum_length_nm", QVariant.Double)
            ])
            layer.updateFields()

            # add features
            feats = []
            crs = self.canvas.mapSettings().destinationCrs()
            wgs84_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            
            for r in final_rows:
                f = QgsFeature(layer.fields())
                # geometry is the segment line
                p1 = QgsPointXY(r['P1x'], r['P1y'])
                p2 = QgsPointXY(r['P2x'], r['P2y'])
                f.setGeometry(QgsGeometry.fromPolylineXY([p1, p2]))
                # prepare WKT strings for Start/Stop POINTs

                # --- Build WKT for start/stop points ---
                if crs.isGeographic():
                    # Already in lon/lat
                    x1, y1 = p1.x(), p1.y()
                    x2, y2 = p2.x(), p2.y()
                else:
                    # Projected CRS (e.g., UTM) → transform to WGS84
                    transform = QgsCoordinateTransform(crs, wgs84_crs, QgsProject.instance())
                    geo_p1 = transform.transform(p1)
                    geo_p2 = transform.transform(p2)
                    x1, y1 = geo_p1.x(), geo_p1.y()
                    x2, y2 = geo_p2.x(), geo_p2.y()

                # Format WKT with decimals
                start_wkt = f"{y1:.4f}, {x1:.4f}"
                stop_wkt  = f"{y2:.4f}, {x2:.4f}"

                f.setAttributes([
                    r.get('Line_ID', None),
                    start_wkt,
                    stop_wkt,
                    r.get('length_m', None),
                    r.get('length_nm', None),
                    r.get('cum_length_m', None),
                    r.get('cum_length_nm', None)
                ])
                feats.append(f)

            pr.addFeatures(feats)
            layer.updateExtents()

            # add final layer to project
            QgsProject.instance().addMapLayer(layer)

            # show attribute table (ONLY NOW)
            self.iface.showAttributeTable(layer)

            # reset
            self.table_rows = []
            self.n = 0
            self.point_id_counter = 0
            self.var_cum_length_m = 0.0
            self.click_count = 0
            self.last_point = None
            self.is_measuring = False

            # remove drawing
            self.main_rb.reset(QgsWkbTypes.LineGeometry)

        def keyPressEvent(self, event):
            # ESC to cancel measurement and remove any temporary artifacts
            if event.key() == Qt.Key_Escape:
                
                self.main_rb.reset(QgsWkbTypes.LineGeometry)
                self.temp_rb.reset(QgsWkbTypes.LineGeometry)
                self.iface.messageBar().pushMessage("AdvancedMeasureTool", "Measurement cancelled", level=Qgis.Info, duration=2)


        ###################################################################
        # SEGMENT CALCULATION
        ###################################################################
        def _measure_length_dynamic(self, geom):
            """Compute length using a fresh QgsDistanceArea object
               to avoid CRS mismatch after CRS changes."""
            da = QgsDistanceArea()
            da.setSourceCrs(self.canvas.mapSettings().destinationCrs(),
                            QgsProject.instance().transformContext())
            da.setEllipsoid(QgsProject.instance().ellipsoid() or "WGS84")
            return da.measureLength(geom)
 
        def calculate_segment(self, idx):
            r = self.table_rows[idx]
            p1 = QgsPointXY(r['P1x'], r['P1y'])
            p2 = QgsPointXY(r['P2x'], r['P2y'])
            geom = QgsGeometry.fromPolylineXY([p1, p2])

            # Use dynamic distance calculator to stay CRS-safe
            length_m = self._measure_length_dynamic(geom)
            length_nm = length_m / 1852.0

            self.var_cum_length_m += length_m
            cum_nm = self.var_cum_length_m / 1852.0

            r['length_m'] = round(length_m, 1)
            r['length_nm'] = round(length_nm, 2)
            r['cum_length_m'] = round(self.var_cum_length_m, 1)
            r['cum_length_nm'] = round(cum_nm, 2)



        ###################################################################
        # START NEW
        ###################################################################
        def start_new_measurement(self):
            self.is_measuring = True
            self.table_rows = []
            self.n = 0
            self.point_id_counter = 0
            self.var_cum_length_m = 0.0
            self.click_count = 1
            self.last_point = None
            self.main_rb.reset(QgsWkbTypes.LineGeometry)
            self.temp_rb.reset(QgsWkbTypes.LineGeometry)
