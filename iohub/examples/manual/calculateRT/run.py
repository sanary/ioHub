from psychopy import visual, core
from iohub.client import launchHubServer,EventConstants
from iohub.util import FullScreenWindow
from math import fabs

io=launchHubServer(psychopy_monitor_name='default')
window=FullScreenWindow(io.devices.display)
# save some 'dots' during the trial loop
keyboard = io.devices.keyboard

# constants for use in example
line_size_match_delay=7.0
full_length=window.size[0]/2
latest_length=0
# Store the RT calculation here
spacebar_rt=0.0

static_bar = visual.ShapeStim(win=window, lineColor='Firebrick', fillColor='Firebrick', vertices= [[0,0],[full_length,0],[full_length,5],[0,5]], pos=(-window.size[0]/4, window.size[1]/24))
expanding_line = visual.ShapeStim(win=window, lineColor='Firebrick', fillColor='Firebrick', vertices= [[0,0],[0,0],[1,0],[0,0]], pos=(-window.size[0]/4, -window.size[1]/24))
text = visual.TextStim(window, text='Press Spacebar When Line Lengths Match', pos = [0,0], height=24, 
                       color=[-1,-1,-1], colorSpace='rgb',alignHoriz='center', alignVert='center',wrapWidth=window.size[0]*.8)
stim=[static_bar,expanding_line,text]

# Draw and Display first frame of screen
[s.draw() for s in stim]
flip_time=window.flip()
io.clearEvents('all')

# Run until space bar is pressed
while spacebar_rt == 0.0:
    #check for RT
    for kb_event in keyboard.getEvents():
        if kb_event.key == ' ':
            spacebar_rt=kb_event.time-flip_time
            break    
    # Update visual stim as needed
    time_passed=core.getTime()-flip_time
    latest_length = time_passed/line_size_match_delay*full_length    
    expanding_line.setPos((-latest_length/2, -window.size[1]/24))
    expanding_line.setVertices([[0,0],[latest_length,0],[latest_length,5],[0,5]])
    
    [s.draw() for s in stim]
    io.clearEvents()
    window.flip()

io.clearEvents('all')

results= "RT: %.4f sec  |||  Perc. Length Diff: %.2f  |||  RT Error: %.4f sec\n"%(spacebar_rt,fabs(latest_length-full_length)/full_length*100.0,spacebar_rt-line_size_match_delay)
exitStr="Press Any Key To Exit"
results=results+exitStr.center(len(results))
text.setText(results)
[s.draw() for s in stim]
window.flip()

while not keyboard.getEvents(event_type_id=EventConstants.KEYBOARD_PRESS):
    io.wait(0.05)
    io.clearEvents()